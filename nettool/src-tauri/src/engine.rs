//! The Python engine as a child process, spoken to in JSON lines.
//!
//! Requests go down the child's stdin as `{"id", "method", "params"}` and come
//! back on its stdout as `{"id", "result"}` or `{"id", "error"}`.  A line with
//! no `id` is something the engine volunteered (the vault locked, a device
//! went down) and is forwarded to the window as an `engine-event`.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::oneshot;

const CALL_TIMEOUT: Duration = Duration::from_secs(30);
const ENGINE_MODULE: &str = "nettool_engine";

type Reply = Result<Value, String>;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Reply>>>>;

/// What the launcher (amvault) told us on stdin: where its credential broker
/// listens, the one-time token for it, and which Python to run the engine with.
///
/// It arrives on stdin rather than in argv or the environment because both of
/// those are readable by other processes for as long as we run; a pipe is read
/// once and gone.
pub fn read_handshake() -> Value {
    // Only when the launcher says so -- started by hand, stdin is a terminal
    // and waiting on it would hang before the window ever appeared.
    if std::env::var("NETTOOL_HANDSHAKE").as_deref() != Ok("stdin") {
        return json!({});
    }
    let mut line = String::new();
    if std::io::stdin().lock().read_line(&mut line).is_err() {
        return json!({});
    }
    serde_json::from_str(line.trim()).unwrap_or_else(|_| json!({}))
}

pub struct Engine {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    pending: Pending,
    next_id: AtomicU64,
    /// Why the engine is not running, if it is not.
    failure: Arc<Mutex<Option<String>>>,
}

impl Engine {
    pub fn start(app: AppHandle, handshake: Value) -> Engine {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let failure = Arc::new(Mutex::new(None));

        match spawn(&handshake) {
            Ok((mut child, mut stdin)) => {
                // First line is the handshake; everything after is requests.
                let sent = writeln!(stdin, "{}", handshake).and_then(|_| stdin.flush());
                if let Err(err) = sent {
                    *failure.lock().unwrap() = Some(format!("engine refused its handshake: {err}"));
                }
                if let Some(stdout) = child.stdout.take() {
                    let pending = pending.clone();
                    let failure = failure.clone();
                    std::thread::spawn(move || {
                        read_loop(BufReader::new(stdout), &app, &pending);
                        // Reaching here means the engine closed its stdout.
                        *failure.lock().unwrap() = Some("the engine stopped".into());
                        for (_, waiter) in pending.lock().unwrap().drain() {
                            let _ = waiter.send(Err("the engine stopped".into()));
                        }
                        let _ = app.emit("engine-event", json!({"event": "engine.stopped"}));
                    });
                }
                Engine {
                    stdin: Mutex::new(Some(stdin)),
                    child: Mutex::new(Some(child)),
                    pending,
                    next_id: AtomicU64::new(1),
                    failure,
                }
            }
            Err(err) => {
                *failure.lock().unwrap() = Some(err);
                Engine {
                    stdin: Mutex::new(None),
                    child: Mutex::new(None),
                    pending,
                    next_id: AtomicU64::new(1),
                    failure,
                }
            }
        }
    }

    pub async fn call(&self, method: &str, params: Value) -> Reply {
        if let Some(reason) = self.failure.lock().unwrap().clone() {
            return Err(reason);
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);

        let line = json!({"id": id, "method": method, "params": params}).to_string();
        let written = match self.stdin.lock().unwrap().as_mut() {
            Some(stdin) => writeln!(stdin, "{line}").and_then(|_| stdin.flush()),
            None => Err(std::io::ErrorKind::BrokenPipe.into()),
        };
        if let Err(err) = written {
            self.pending.lock().unwrap().remove(&id);
            return Err(format!("could not reach the engine: {err}"));
        }

        match tokio::time::timeout(CALL_TIMEOUT, rx).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err("the engine stopped".into()),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(format!("{method} timed out"))
            }
        }
    }

    pub fn stop(&self) {
        // Closing stdin is the polite request; the engine exits on EOF.
        self.stdin.lock().unwrap().take();
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn read_loop(reader: impl BufRead, app: &AppHandle, pending: &Pending) {
    for line in reader.lines() {
        let Ok(line) = line else { break };
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        match message.get("id").and_then(Value::as_u64) {
            Some(id) => {
                let Some(waiter) = pending.lock().unwrap().remove(&id) else {
                    continue;
                };
                let reply = match message.get("error") {
                    Some(error) => Err(error
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| error.to_string())),
                    None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
                };
                let _ = waiter.send(reply);
            }
            None => {
                let _ = app.emit("engine-event", message);
            }
        }
    }
}

/// The directory holding the `nettool_engine` package.
///
/// Found by walking up from the executable, so the same binary works from
/// `src-tauri/target/release/` during development and from the project root
/// when copied there.
fn engine_root() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("NETTOOL_ENGINE_DIR") {
        return Some(PathBuf::from(dir));
    }
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .skip(1)
        .find(|dir| dir.join(ENGINE_MODULE).join("__main__.py").is_file())
        .map(PathBuf::from)
}

/// The self-contained engine an installer puts beside the app.
fn bundled_engine() -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "nettool-engine.exe"
    } else {
        "nettool-engine"
    };
    let path = std::env::current_exe().ok()?.parent()?.join(name);
    path.is_file().then_some(path)
}

fn spawn(handshake: &Value) -> Result<(Child, ChildStdin), String> {
    // An installed copy brings its own engine and needs no Python at all.
    // Pointing NETTOOL_ENGINE_DIR at a source tree overrides it, for
    // trying engine changes against an installed app.
    if std::env::var("NETTOOL_ENGINE_DIR").is_err() {
        if let Some(engine) = bundled_engine() {
            let mut command = Command::new(&engine);
            return start(command_io(&mut command), &engine.to_string_lossy());
        }
    }

    let root = engine_root()
        .ok_or_else(|| format!("could not find the {ENGINE_MODULE} folder next to the app"))?;

    // The tool's own environment first: that is where its libraries (SSH) are
    // installed.  The launcher's interpreter is the fallback, good enough for
    // everything that needs only the standard library.
    let own = root.join(".venv").join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    });
    let python = std::env::var("NETTOOL_PYTHON")
        .ok()
        .or_else(|| own.is_file().then(|| own.to_string_lossy().into_owned()))
        .or_else(|| {
            handshake
                .get("python")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.to_owned());

    let mut command = Command::new(&python);
    command.args(["-m", ENGINE_MODULE]).current_dir(&root);
    start(command_io(&mut command), &python)
}

/// Wire a command up as the engine: protocol on stdin and stdout, messages
/// for a person on stderr.
fn command_io(command: &mut Command) -> &mut Command {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: the engine is a console program and would
        // otherwise flash a console window beside ours.
        command.creation_flags(0x0800_0000);
    }
    command
}

fn start(command: &mut Command, program: &str) -> Result<(Child, ChildStdin), String> {
    let mut child = command
        .spawn()
        .map_err(|err| format!("could not start the engine with `{program}`: {err}"))?;
    let stdin = child.stdin.take().ok_or("engine has no stdin")?;
    Ok((child, stdin))
}
