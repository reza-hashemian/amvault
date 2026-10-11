"""Open an SSH session to the host stored in an entry.

The point is to remove the copy-paste step: the entry already knows the host,
port, user and which key to use, so connecting should be one action rather than
four clipboard round-trips.

The key is loaded into ssh-agent first and removed when the session ends, so it
is available for exactly as long as the connection needs it.  A terminal is
launched because SSH is interactive -- it may ask to confirm a host key, or the
session itself is the point.

Where no agent can be reached the key is instead staged as a temporary file
readable only by the current user, and deleted when the session ends.  That is
the normal path on Windows, whose agent is a service that ships disabled and
needs Administrator to enable -- refusing to connect there would make key auth
unusable for most users, which is a worse trade than the short-lived file.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import sys

from .models import Entry
from .ssh_agent import (
    IS_WINDOWS, SshAgentError, add_key, agent_available, agent_hint,
    fingerprint_of, remove_key, _restrict_to_owner,
)

DEFAULT_SSH_PORT = 22


class SshConnectError(Exception):
    pass


def build_ssh_command(entry: Entry, identity_path: str | None = None) -> list[str]:
    """Assemble the ssh command from an entry's fields.

    Raises SshConnectError naming the missing field, so the UI can say exactly
    what to fill in rather than failing with ssh's own error.

    ssh-agent has no idea which key belongs to which host -- it simply offers
    every key it holds, in turn. With several keys loaded a server can hit its
    `MaxAuthTries` limit and refuse the connection before reaching the right
    one. Passing the specific key restricts the attempt to the one this entry
    carries.

    `identity_path` is the public half when the key went into the agent, and
    the private half when it did not; ssh accepts either in `IdentityFile`.
    """
    fields = entry.fields
    host = (fields.get("host") or fields.get("mgmt_ip") or fields.get("url") or "").strip()
    if not host:
        raise SshConnectError(
            "This entry has no host. Add a `host` field (or `mgmt_ip` for a "
            "network device) with the address to connect to."
        )

    # Tolerate a pasted "user@host" or "ssh://host" in the host field.
    if host.startswith("ssh://"):
        host = host[len("ssh://"):]
    host = host.rstrip("/")

    user = (fields.get("username") or "").strip()
    if "@" in host:
        embedded_user, host = host.split("@", 1)
        user = user or embedded_user

    port = (fields.get("port") or fields.get("mgmt_port") or "").strip()
    if ":" in host and not host.startswith("["):
        # host:port, but leave bracketed IPv6 literals alone.
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            host, port = maybe_host, port or maybe_port

    command = ["ssh"]
    if port and port.isdigit() and int(port) != DEFAULT_SSH_PORT:
        command += ["-p", port]

    if identity_path:
        # Either half works here. Given the public half, ssh matches it against
        # the agent's keys and offers only the corresponding private one; given
        # the private half (the no-agent fallback) it uses the file directly.
        # IdentitiesOnly stops it walking every other key first either way.
        command += [
            "-o", "IdentitiesOnly=yes",
            "-o", f"IdentityFile={identity_path}",
        ]

    command.append(f"{user}@{host}" if user else host)
    return command


def describe_target(entry: Entry) -> str:
    """A human-readable 'user@host:port' for confirmation prompts."""
    try:
        command = build_ssh_command(entry)
    except SshConnectError:
        return "?"
    target = command[-1]
    if "-p" in command:
        target += ":" + command[command.index("-p") + 1]
    return target


# Terminal emulators, in the order we try them. The argument that introduces a
# command differs per terminal, which is why this is a table and not a guess.
#
# `x-terminal-emulator` is deliberately absent: it is a Debian alternatives
# symlink that can point at any of these, so it carries no flag of its own.
# find_terminal() resolves it to whichever terminal it names and uses that
# entry's flag -- passing a guessed one launches the wrong kind of window.
_TERMINALS = [
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["--"]),
    ("tilix", ["-e"]),
    ("alacritty", ["-e"]),
    ("kitty", []),
    ("wezterm", ["start", "--"]),
    ("foot", []),
    ("terminator", ["-x"]),
    ("ptyxis", ["--"]),
    ("xterm", ["-e"]),
]

def _resolve_alternative(path: str) -> tuple[str, list[str]] | None:
    """Map the x-terminal-emulator symlink onto a known terminal.

    The symlink is followed to its target and matched by basename, because the
    flag that introduces a command belongs to the real terminal, not to the
    alternatives name. Returns None when it points somewhere unrecognised, so
    the caller can fall through to probing the table directly.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    name = os.path.basename(target)
    for candidate, prefix in _TERMINALS:
        if name == candidate:
            return path, prefix
        if name.startswith(candidate + "."):
            # Debian's wrappers (gnome-terminal.wrapper, xfce4-terminal.wrapper)
            # exist to make the terminal answer to xterm's options, so what
            # they understand is `-e` -- not the terminal's own flag. Handed
            # gnome-terminal's `--`, the wrapper drops it and the command with
            # it, and the window opens on a bare shell with no ssh in it.
            return path, ["-e"]
    return None


def find_terminal() -> tuple[str, list[str]] | None:
    """Pick a terminal emulator and the flag that makes it run a command.

    The user's configured default (`x-terminal-emulator`) is preferred so the
    session opens in the terminal they actually use, but only when we can tell
    what it is -- an unknown one falls through to the table.
    """
    default = shutil.which("x-terminal-emulator")
    if default:
        resolved = _resolve_alternative(default)
        if resolved:
            return resolved

    for name, prefix in _TERMINALS:
        path = shutil.which(name)
        if path:
            return path, prefix
    return None


def login_shell() -> str | None:
    """The user's own shell, so the session inherits their terminal config.

    $SHELL is trusted over the passwd entry: a user who runs a shell other than
    their login one sets it, and that is the config they expect to see.
    """
    shell = (os.environ.get("SHELL") or "").strip()
    if shell and os.path.isfile(shell) and os.access(shell, os.X_OK):
        return shell
    try:
        import pwd

        shell = pwd.getpwuid(os.getuid()).pw_shell or ""
    except (ImportError, KeyError, OSError):
        return None
    return shell if shell and os.access(shell, os.X_OK) else None


def wrap_in_shell(ssh_command: list[str], env: dict | None = None) -> list[str]:
    """Run ssh through the user's shell, holding the window open on failure.

    A terminal launched with `-e ssh ...` makes ssh the terminal's own child,
    so no shell ever starts and the window comes up without the user's prompt,
    colours or aliases.  Running it through `$SHELL -c` loads that config
    first.

    Two details are deliberate:

    `-i` is NOT passed.  An interactive shell expects to own the terminal, and
    prompt frameworks (powerlevel10k, gitstatus) abort with `can't change
    option: zle` when they find they do not -- noise before ssh even starts.
    A non-interactive shell still reads the config that matters here.

    ssh is not `exec`d.  When it exits the shell stays alive long enough to
    report why: without this the window closes the instant the connection
    fails, taking "no route to host" or "permission denied" with it faster
    than it can be read.  A clean exit closes immediately, as it should.
    """
    shell = login_shell()
    if not shell:
        if env:
            return ["env", *(f"{name}={value}" for name, value in env.items()), *ssh_command]
        return ssh_command
    quoted = " ".join(shlex.quote(part) for part in ssh_command)
    if env:
        # Set on ssh's own command line rather than in our environment: many
        # terminals hand the command to an already-running server process,
        # which starts it with its environment, not ours.
        quoted = " ".join(f"{name}={shlex.quote(value)}" for name, value in env.items()) + " " + quoted
    # `status` is read-only in zsh (an alias for $?), so the variable holding
    # the exit code must not use that name -- assigning to it aborts the script
    # and closes the window, which is the very thing this is here to prevent.
    script = (
        f"{quoted}\n"
        "amvault_rc=$?\n"
        "if [ \"$amvault_rc\" -ne 0 ]; then\n"
        "  printf '\\n[amvault] ssh exited with status %s.\\n' \"$amvault_rc\"\n"
        "  printf 'Press Enter to close this window.'\n"
        "  read _\n"
        "fi\n"
        "exit $amvault_rc\n"
    )
    return [shell, "-c", script]


def _launch_linux(ssh_command: list[str], title: str, env: dict | None = None) -> subprocess.Popen:
    terminal = find_terminal()
    if terminal is None:
        raise SshConnectError(
            "No terminal emulator found. Install one (for example xterm), or "
            "use the command shown below in your own terminal."
        )
    path, prefix = terminal
    return subprocess.Popen([path, *prefix, *wrap_in_shell(ssh_command, env)])


def _launch_macos(ssh_command: list[str], title: str, env: dict | None = None) -> subprocess.Popen:
    # Terminal.app runs the string through the user's own shell already, so
    # unlike Linux this needs no wrapping -- just correct quoting.
    quoted = " ".join(shlex.quote(part) for part in ssh_command)
    if env:
        quoted = " ".join(f"{name}={shlex.quote(value)}" for name, value in env.items()) + " " + quoted
    script = f'tell application "Terminal" to do script {quoted!r}\n' \
             'tell application "Terminal" to activate'
    return subprocess.Popen(["osascript", "-e", script])


def _launch_windows(ssh_command: list[str], title: str, env: dict | None = None) -> subprocess.Popen:
    quoted = subprocess.list2cmdline(ssh_command)
    # Windows Terminal if present, otherwise a plain console window. `start`
    # needs a title argument before the command, or it treats the first quoted
    # token as the title.
    #
    # With variables to pass, always the plain console: Windows Terminal opens
    # the tab from its already-running process, which would not carry them.
    if shutil.which("wt.exe") and not env:
        return subprocess.Popen(["wt.exe", "-w", "0", "nt", *ssh_command])
    return subprocess.Popen(
        f'start "{title}" cmd /k {quoted}',
        shell=True,
        env={**os.environ, **env} if env else None,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def launch_terminal(ssh_command: list[str], title: str = "amvault",
                    env: dict | None = None) -> subprocess.Popen:
    """Open a terminal running `ssh_command`, with `env` added to ssh's
    environment."""
    if IS_WINDOWS:
        return _launch_windows(ssh_command, title, env)
    if sys.platform == "darwin":
        return _launch_macos(ssh_command, title, env)
    return _launch_linux(ssh_command, title, env)


def ssh_available() -> bool:
    return shutil.which("ssh") is not None


def _write_public_key(private_key: bytes, passphrase: str | None) -> str | None:
    """Extract the public half of a key and leave it in a temp file.

    A public key is not a secret, so unlike the private key this one may sit on
    disk for the life of the session -- ssh needs to read it when the terminal
    starts, which is after this function has returned.

    Returns None if the public half cannot be derived; the connection then
    falls back to offering every agent key, which still works.
    """
    directory = tempfile.mkdtemp(prefix="amvault-pub-")
    private_path = os.path.join(directory, "k")
    public_path = private_path + ".pub"
    try:
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, private_key)
            if not private_key.endswith(b"\n"):
                os.write(fd, b"\n")
        finally:
            os.close(fd)

        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", private_path,
             "-P", passphrase if passphrase else ""],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            shutil.rmtree(directory, ignore_errors=True)
            return None

        with open(public_path, "wb") as fh:
            fh.write(result.stdout)
        return public_path
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(directory, ignore_errors=True)
        return None
    finally:
        # The private copy goes immediately; only the public half remains.
        try:
            os.unlink(private_path)
        except OSError:
            pass


def _write_private_key(private_key: bytes) -> str | None:
    """Stage the private key in a locked-down directory for ssh to read.

    This is the fallback for when no ssh-agent is reachable -- most often on
    Windows, where the agent is a service that ships disabled and enabling it
    needs Administrator. Without this the connection simply fails, so trading
    the RAM-only guarantee for a working login is the better outcome; callers
    are told through `ram_backed: False` and the file is removed by
    `cleanup_key_file` once the session ends.

    The directory is created 0700 and, on Windows, has inherited ACLs stripped
    so only the current user can read it. ssh itself refuses a key file that
    other users can read, which is the same check from the other direction.
    """
    directory = tempfile.mkdtemp(prefix="amvault-key-")
    os.chmod(directory, 0o700)
    _restrict_to_owner(directory)
    key_path = os.path.join(directory, "id_vault")
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, private_key)
            # OpenSSH rejects a key file whose final newline is missing.
            if not private_key.endswith(b"\n"):
                os.write(fd, b"\n")
        finally:
            os.close(fd)
        _restrict_to_owner(key_path)
        return key_path
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        return None


def cleanup_key_file(key_path: str | None) -> None:
    """Remove a staged key file and the directory holding it.

    Safe to call with None or an already-deleted path, so callers can run it
    unconditionally when a session ends.
    """
    if not key_path:
        return
    shutil.rmtree(os.path.dirname(key_path), ignore_errors=True)


# Terminals that spawn a console and exit immediately (`start` on Windows,
# gnome-terminal talking to its server on Linux) leave us holding a process
# that is already dead while ssh is only just starting. Deleting the key on
# that signal alone would pull it out from under ssh before it is read, so the
# wait never ends sooner than this many seconds.
_KEY_FILE_GRACE = 120


def _cleanup_when_done(process: subprocess.Popen, key_path: str,
                       max_wait: int = 86400) -> None:
    """Delete the staged key once the terminal it was staged for exits.

    A daemon thread, so it never holds the application open. The key file is
    the fallback's whole weakness, and this is what bounds it: without the
    wait the file would have to be deleted immediately (breaking the
    connection it exists for) or left behind indefinitely.

    The floor of `_KEY_FILE_GRACE` matters more than the ceiling. A forking
    terminal reports its own exit within milliseconds of launch, long before
    ssh has opened the key file, so honouring that exit literally would break
    exactly the connection this file exists to make.

    `max_wait` is the backstop for a terminal left open for days -- ssh read
    the key at startup, so removing it then costs the session nothing.
    """
    def wait_and_clean() -> None:
        start = time.monotonic()
        try:
            process.wait(timeout=max_wait)
        except Exception:
            # Timed out, or the process is not waitable. Either way ssh has
            # long had the key, so fall through and remove it.
            pass
        # Hold the floor even when the terminal already exited.
        remaining = _KEY_FILE_GRACE - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(remaining)
        cleanup_key_file(key_path)

    threading.Thread(target=wait_and_clean, daemon=True).start()


def connect(
    entry: Entry,
    key_bytes: bytes | None = None,
    passphrase: str | None = None,
    lifetime_seconds: int = 900,
    password: str | None = None,
) -> dict:
    """Load the key (if any) and open a terminal session to the entry's host.

    The key is left in the agent for `lifetime_seconds` rather than removed
    when this returns: the terminal runs independently, and pulling the key out
    immediately would break the connection it was loaded for.  The agent
    expires it on its own, which is what that timeout is for.
    """
    if not ssh_available():
        raise SshConnectError(
            "The `ssh` command was not found. On Windows, install OpenSSH from "
            "Settings > Optional Features."
        )

    loaded = None
    identity_path = None
    key_file_path = None
    agent_error = None

    if key_bytes:
        # Preferred path: the agent holds the key, and ssh is pointed at the
        # public half so it asks for exactly this one. Nothing private touches
        # disk for longer than a few milliseconds.
        if agent_available():
            try:
                loaded = add_key(
                    key_bytes,
                    passphrase=passphrase,
                    lifetime_seconds=lifetime_seconds or None,
                    comment=entry.title.replace(" ", "_") or "amvault",
                )
                identity_path = _write_public_key(key_bytes, passphrase)
            except SshAgentError as exc:
                agent_error = str(exc)
        else:
            agent_error = agent_hint()

        if loaded is None:
            # Fallback: hand ssh the private key directly. Weaker, but it is
            # the difference between connecting and not -- and on Windows the
            # agent is disabled by default, so this is the common path there.
            key_file_path = _write_private_key(key_bytes)
            identity_path = key_file_path
            if key_file_path is None:
                raise SshConnectError(
                    f"Could not use this key.\n\n{agent_error}\n\n"
                    "Writing a temporary key file failed as well, so there is "
                    "no way to offer the key to ssh."
                )

    ssh_command = build_ssh_command(entry, identity_path)

    # With a password in the entry, ssh collects it from us when the server
    # asks, so the session signs in without it being typed. See ssh_askpass.
    offer = None
    if password:
        from .ssh_askpass import PasswordOffer
        try:
            offer = PasswordOffer(password)
        except OSError:
            offer = None      # ssh will ask in the terminal instead

    try:
        process = launch_terminal(ssh_command, title=entry.title,
                                  env=offer.environment() if offer else None)
    except Exception:
        # Nothing will ever consume the key now, so do not leave it behind.
        cleanup_key_file(key_file_path)
        if offer:
            offer.close()
        raise

    if key_file_path:
        _cleanup_when_done(process, key_file_path)

    return {
        "command": ssh_command,
        "display": " ".join(ssh_command),
        "key_loaded": loaded is not None or key_file_path is not None,
        "password_supplied": offer is not None,
        "via_agent": loaded is not None,
        "key_file": key_file_path,
        "agent_error": agent_error,
        "fingerprint": loaded["fingerprint"] if loaded else fingerprint_of(key_bytes) if key_bytes else None,
        "ram_backed": loaded["ram_backed"] if loaded else False,
        "lifetime": lifetime_seconds,
        "pid": process.pid,
    }
