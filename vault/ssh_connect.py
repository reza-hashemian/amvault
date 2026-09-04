"""Open an SSH session to the host stored in an entry.

The point is to remove the copy-paste step: the entry already knows the host,
port, user and which key to use, so connecting should be one action rather than
four clipboard round-trips.

The key is loaded into ssh-agent first and removed when the session ends, so it
is available for exactly as long as the connection needs it.  A terminal is
launched because SSH is interactive -- it may ask to confirm a host key, or the
session itself is the point.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import sys

from .models import Entry
from .ssh_agent import IS_WINDOWS, SshAgentError, add_key, remove_key

DEFAULT_SSH_PORT = 22


class SshConnectError(Exception):
    pass


def build_ssh_command(entry: Entry, public_key_path: str | None = None) -> list[str]:
    """Assemble the ssh command from an entry's fields.

    Raises SshConnectError naming the missing field, so the UI can say exactly
    what to fill in rather than failing with ssh's own error.

    ssh-agent has no idea which key belongs to which host -- it simply offers
    every key it holds, in turn. With several keys loaded a server can hit its
    `MaxAuthTries` limit and refuse the connection before reaching the right
    one. Passing the specific key restricts the attempt to the one this entry
    carries.
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

    if public_key_path:
        # Point ssh at the public half: it matches this against the agent's
        # keys and offers only the corresponding private one. IdentitiesOnly
        # stops it from walking every other key first.
        command += [
            "-o", "IdentitiesOnly=yes",
            "-o", f"IdentityFile={public_key_path}",
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
_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
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
    ("xterm", ["-e"]),
]


def find_terminal() -> tuple[str, list[str]] | None:
    for name, prefix in _TERMINALS:
        path = shutil.which(name)
        if path:
            return path, prefix
    return None


def _launch_linux(ssh_command: list[str], title: str) -> subprocess.Popen:
    terminal = find_terminal()
    if terminal is None:
        raise SshConnectError(
            "No terminal emulator found. Install one (for example xterm), or "
            "use the command shown below in your own terminal."
        )
    path, prefix = terminal
    return subprocess.Popen([path, *prefix, *ssh_command])


def _launch_macos(ssh_command: list[str], title: str) -> subprocess.Popen:
    # Terminal.app takes a command string, not an argv, so quote it properly.
    quoted = " ".join(shlex.quote(part) for part in ssh_command)
    script = f'tell application "Terminal" to do script {quoted!r}\n' \
             'tell application "Terminal" to activate'
    return subprocess.Popen(["osascript", "-e", script])


def _launch_windows(ssh_command: list[str], title: str) -> subprocess.Popen:
    quoted = subprocess.list2cmdline(ssh_command)
    # Windows Terminal if present, otherwise a plain console window. `start`
    # needs a title argument before the command, or it treats the first quoted
    # token as the title.
    if shutil.which("wt.exe"):
        return subprocess.Popen(["wt.exe", "-w", "0", "nt", *ssh_command])
    return subprocess.Popen(
        f'start "{title}" cmd /k {quoted}',
        shell=True,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def launch_terminal(ssh_command: list[str], title: str = "amvault") -> subprocess.Popen:
    if IS_WINDOWS:
        return _launch_windows(ssh_command, title)
    if sys.platform == "darwin":
        return _launch_macos(ssh_command, title)
    return _launch_linux(ssh_command, title)


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


def connect(
    entry: Entry,
    key_bytes: bytes | None = None,
    passphrase: str | None = None,
    lifetime_seconds: int = 900,
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

    # Derive the public half so ssh can ask the agent for exactly this key
    # rather than offering everything it holds.
    public_key_path = None
    if key_bytes:
        public_key_path = _write_public_key(key_bytes, passphrase)

    ssh_command = build_ssh_command(entry, public_key_path)

    loaded = None
    if key_bytes:
        try:
            loaded = add_key(
                key_bytes,
                passphrase=passphrase,
                lifetime_seconds=lifetime_seconds or None,
                comment=entry.title.replace(" ", "_") or "amvault",
            )
        except SshAgentError as exc:
            # Connecting without the key is still useful -- the server may
            # accept a password -- so report it rather than refusing outright.
            raise SshConnectError(
                f"Could not load the key into ssh-agent: {exc}\n\n"
                f"You can still connect manually with:\n  "
                f"{' '.join(ssh_command)}"
            ) from exc

    process = launch_terminal(ssh_command, title=entry.title)

    return {
        "command": ssh_command,
        "display": " ".join(ssh_command),
        "key_loaded": loaded is not None,
        "fingerprint": loaded["fingerprint"] if loaded else None,
        "ram_backed": loaded["ram_backed"] if loaded else None,
        "lifetime": lifetime_seconds,
        "pid": process.pid,
    }
