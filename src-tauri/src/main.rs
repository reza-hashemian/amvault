// The shell is deliberately thin: it owns the window and relays requests to
// the Python engine, which is where all the network logic lives.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod engine;

use engine::Engine;
use serde_json::Value;
use tauri::{Manager, State};

#[tauri::command]
async fn engine_call(
    engine: State<'_, Engine>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    engine.call(&method, params.unwrap_or(Value::Null)).await
}

fn main() {
    // Read before the window exists: the launcher is waiting on this pipe.
    let handshake = engine::read_handshake();

    tauri::Builder::default()
        .setup(move |app| {
            app.manage(Engine::start(app.handle().clone(), handshake));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![engine_call])
        .build(tauri::generate_context!())
        .expect("failed to start nettool")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<Engine>().stop();
            }
        });
}
