"""Load an SSH key from the vault into ssh-agent for as long as it is needed.

The point is that the private key never lands on persistent storage.  Keys are
written to a private directory under /dev/shm -- a tmpfs, so the bytes live in
RAM and vanish on reboot -- handed to `ssh-add`, and unlinked immediately.  Once
ssh-agent holds the key, the file is not needed any more, so its lifetime is
measured in milliseconds.

Nothing about the host's own keys is touched.  ssh-agent holds several keys at
once and offers them in turn, so adding a vault key does not displace the key
you normally use; removing it afterwards restores the agent to exactly the set
it had before.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile

IS_WINDOWS = sys.platform == "win32"

# tmpfs candidates in preference order. /dev/shm is RAM on every mainstream
# Linux; the XDG runtime dir is usually tmpfs too and is per-user. Windows has
# no RAM-backed filesystem available to user code, so there the key is written
# to the user's private temp directory and deleted immediately -- the guarantee
# is weaker, and callers are told so through `ram_backed`.
_TMPFS_CANDIDATES = () if IS_WINDOWS else ("/dev/shm", os.environ.get("XDG_RUNTIME_DIR") or "")


class SshAgentError(Exception):
    pass


def agent_available() -> bool:
    """True if an ssh-agent is reachable.

    Exit code 2 means "cannot connect"; 0 means keys are loaded and 1 means the
    agent is running but empty -- both of which count as available.

    On Windows the agent is a service that ships disabled, so a failure here is
    usually "not started yet" rather than "not installed". start_windows_agent()
    is tried once before giving up, which turns the common case into a working
    connection instead of an error the user has to go and fix by hand.
    """
    if not shutil.which("ssh-add"):
        return False
    if _agent_responds():
        return True
    if IS_WINDOWS and start_windows_agent():
        return _agent_responds()
    return False


def _agent_responds() -> bool:
    """Ask the agent directly, without trying to start anything."""
    try:
        result = subprocess.run(
            ["ssh-add", "-l"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode != 2


def windows_agent_state() -> str | None:
    """The ssh-agent service's state: 'running', 'stopped', or None if absent.

    `sc query` prints a line like "STATE : 4  RUNNING". Parsing the word rather
    than the number keeps this readable; a missing service exits non-zero, which
    is reported as None so the caller can say "not installed" specifically.
    """
    if not IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["sc", "query", "ssh-agent"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").upper()
    if "RUNNING" in text:
        return "running"
    if "STOPPED" in text or "STOP_PENDING" in text:
        return "stopped"
    return "unknown"


def start_windows_agent() -> bool:
    """Try to start the ssh-agent service, returning whether it now runs.

    The service ships with start type Disabled, and `sc start` on a disabled
    service fails with "service cannot be started" no matter the privileges --
    so the start type is set to automatic first. Both calls need Administrator;
    without it they fail harmlessly and the caller falls back to a key file.
    """
    if not IS_WINDOWS:
        return False
    if windows_agent_state() is None:
        return False
    try:
        subprocess.run(
            ["sc", "config", "ssh-agent", "start=", "auto"],
            capture_output=True, timeout=15, check=False,
        )
        subprocess.run(
            ["sc", "start", "ssh-agent"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return windows_agent_state() == "running"


def agent_hint() -> str:
    """Platform-correct instructions for getting an agent running.

    `eval $(ssh-agent)` is meaningless in cmd.exe and PowerShell, so Windows
    gets the service commands it actually needs.
    """
    if not IS_WINDOWS:
        return "No ssh-agent is running. Start one with:  eval $(ssh-agent)"
    if windows_agent_state() is None:
        return (
            "OpenSSH is not installed. Add it from "
            "Settings > Apps > Optional Features > OpenSSH Client."
        )
    return (
        "The OpenSSH Authentication Agent service is not running. Start it "
        "from an Administrator PowerShell with:\n"
        "  Set-Service ssh-agent -StartupType Automatic\n"
        "  Start-Service ssh-agent"
    )


def _ram_backed_dir() -> str:
    """Create a private directory, on a RAM-backed filesystem where one exists.

    Falls back to the normal temp dir otherwise; the caller is told through
    `ram_backed`, because that fallback means the key does touch a real disk.
    """
    for base in _TMPFS_CANDIDATES:
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return tempfile.mkdtemp(prefix="amvault-", dir=base)

    directory = tempfile.mkdtemp(prefix="amvault-")
    if IS_WINDOWS:
        _restrict_to_owner(directory)
    return directory


def _restrict_to_owner(path: str) -> None:
    """Limit a path to the current user on Windows.

    mkdtemp already creates under the per-user temp directory, but an explicit
    ACL removes inherited permissions rather than trusting the default.
    """
    if not IS_WINDOWS:
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r",
             f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Not fatal: the file is deleted within milliseconds either way.
        pass


def is_ram_backed(path: str) -> bool:
    return any(path.startswith(base) for base in _TMPFS_CANDIDATES if base)


def list_agent_fingerprints() -> set[str]:
    """Fingerprints currently held by the agent.

    Used to tell whether a key we are about to add is already there, and to
    confirm afterwards that we removed exactly what we added.
    """
    try:
        result = subprocess.run(
            ["ssh-add", "-l"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()

    fingerprints = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # Format: "<bits> <fingerprint> <comment> (<type>)"
        if len(parts) >= 2 and parts[1].startswith("SHA256:"):
            fingerprints.add(parts[1])
    return fingerprints


def fingerprint_of(key_bytes: bytes) -> str | None:
    """Compute a key's fingerprint without adding it to the agent."""
    directory = _ram_backed_dir()
    path = os.path.join(directory, "k")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key_bytes)
        finally:
            os.close(fd)
        result = subprocess.run(
            ["ssh-keygen", "-lf", path], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        return parts[1] if len(parts) >= 2 else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def add_key(
    key_bytes: bytes,
    passphrase: str | None = None,
    lifetime_seconds: int | None = None,
    comment: str | None = None,
) -> dict:
    """Add a private key to ssh-agent without leaving it on disk.

    `lifetime_seconds` asks the agent to drop the key by itself, which is the
    safety net for when a session ends without cleanup running.

    Returns a dict with the fingerprint and where the key was staged.
    """
    if not shutil.which("ssh-add"):
        raise SshAgentError("ssh-add is not installed.")
    if not agent_available():
        raise SshAgentError(agent_hint())

    directory = _ram_backed_dir()
    ram_backed = is_ram_backed(directory)
    key_path = os.path.join(directory, comment or "id_vault")

    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key_bytes)
            # OpenSSH rejects a key file whose final newline is missing.
            if not key_bytes.endswith(b"\n"):
                os.write(fd, b"\n")
        finally:
            os.close(fd)

        cmd = ["ssh-add"]
        if lifetime_seconds:
            cmd += ["-t", str(lifetime_seconds)]
        cmd.append(key_path)

        if passphrase:
            # ssh-add reads a passphrase from a helper program, never stdin, so
            # SSH_ASKPASS is the only way to supply one non-interactively.
            result = _run_with_askpass(cmd, passphrase)
        else:
            env = dict(os.environ)
            # Without this ssh-add would block on a passphrase prompt if the
            # key turns out to be encrypted.
            env["SSH_ASKPASS_REQUIRE"] = "never"
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                    env=env, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise SshAgentError(
                    "ssh-add did not finish in time. If this key is encrypted, "
                    "store its passphrase in the entry's `passphrase` field."
                ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            lowered = stderr.lower()
            if ("incorrect passphrase" in lowered or "bad pass" in lowered
                    or (passphrase and not stderr)):
                # ssh-add stays silent when the askpass helper returns a wrong
                # passphrase, so a silent failure with a passphrase supplied is
                # almost always exactly that.
                raise SshAgentError(
                    "The passphrase for this key was rejected. Check the "
                    "entry's `passphrase` field."
                )
            if not stderr:
                raise SshAgentError(
                    "ssh-add rejected the key. If it is encrypted, store its "
                    "passphrase in the entry's `passphrase` field."
                )
            raise SshAgentError(f"ssh-add failed: {stderr}")

    finally:
        # The agent has its own copy now, so remove ours whether or not the add
        # succeeded.
        shutil.rmtree(directory, ignore_errors=True)

    return {
        "fingerprint": fingerprint_of(key_bytes),
        "ram_backed": ram_backed,
        "lifetime": lifetime_seconds,
    }


def _run_with_askpass(cmd: list[str], passphrase: str):
    """Run ssh-add with a throwaway SSH_ASKPASS helper that echoes a passphrase.

    The helper script lives in RAM and is deleted immediately.  The passphrase
    is passed to it through the environment rather than written into the script,
    so it never reaches a file.

    Windows OpenSSH ignores SSH_ASKPASS entirely, so an encrypted key cannot be
    supplied a passphrase non-interactively there.
    """
    if IS_WINDOWS:
        # Not fatal: ssh_connect falls back to a key file, and ssh then asks
        # for the passphrase in the terminal it opens -- which is a perfectly
        # normal way to use an encrypted key, just not an automatic one.
        raise SshAgentError(
            "This key is encrypted, and Windows OpenSSH cannot be given a "
            "passphrase automatically. ssh will ask for it in the terminal."
        )
    directory = _ram_backed_dir()
    helper = os.path.join(directory, "askpass")
    try:
        # ssh-add re-invokes the helper on a wrong passphrase, which would
        # loop until the timeout. Answer once, then fail so it gives up.
        marker = os.path.join(directory, "asked")
        script = (
            '#!/bin/sh\n'
            f'if [ -e "{marker}" ]; then exit 1; fi\n'
            f': > "{marker}"\n'
            'printf "%s\\n" "$AMVAULT_PASSPHRASE"\n'
        )
        fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.write(fd, script.encode())
        finally:
            os.close(fd)

        env = dict(os.environ)
        env["SSH_ASKPASS"] = helper
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["AMVAULT_PASSPHRASE"] = passphrase
        env["DISPLAY"] = env.get("DISPLAY", ":0")

        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                env=env, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshAgentError(
                "ssh-add did not finish in time — usually a wrong passphrase. "
                "Check the entry's `passphrase` field."
            ) from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def remove_key(key_bytes: bytes) -> bool:
    """Remove a specific key from the agent, leaving every other key alone."""
    directory = _ram_backed_dir()
    key_path = os.path.join(directory, "k")
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key_bytes)
            if not key_bytes.endswith(b"\n"):
                os.write(fd, b"\n")
        finally:
            os.close(fd)

        env = dict(os.environ)
        env["SSH_ASKPASS_REQUIRE"] = "never"
        result = subprocess.run(
            ["ssh-add", "-d", key_path], capture_output=True, text=True,
            timeout=15, env=env, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(directory, ignore_errors=True)
