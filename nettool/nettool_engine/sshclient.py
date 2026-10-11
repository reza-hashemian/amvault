"""An SSH session to a device, for reading its vital signs.

Two ways of running commands, because devices differ: servers and RouterOS
accept one `exec` request after another on a single connection, while Cisco
IOS closes the connection after the first, so it is driven through an
interactive session and its prompt is watched for instead.

Host keys are trusted on first use and pinned.  A device whose key later
changes is refused, loudly: silently accepting it is exactly what would let
someone on the path collect the password this module is about to send.
"""

from __future__ import annotations

import io
import os
import re
import socket
import threading
import time

from .paths import data_dir

CONNECT_TIMEOUT_SECONDS = 8
COMMAND_TIMEOUT_SECONDS = 12
# A device prompt at the end of what has been received: "Switch#", "R1(config)#",
# "sw-floor1>".  Anchored to the end so a "#" inside output does not match.
_PROMPT = re.compile(r"[\r\n][\w.\-/:()@]+[>#]\s?$")
_PASSWORD = re.compile(r"[Pp]assword:\s*$")
_known_hosts_lock = threading.Lock()


class SshError(Exception):
    """Anything that stops a session; the message is shown on the dashboard."""


def _paramiko():
    try:
        import paramiko
    except ImportError:
        raise SshError(
            "The SSH library is not installed. In the net-tool folder run: "
            "python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
        ) from None
    return paramiko


def known_hosts_path() -> str:
    return os.path.join(data_dir(), "known_hosts")


def _load_key(paramiko, text: str, passphrase: str | None):
    last = None
    for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return kind.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise SshError("The SSH key is encrypted and the entry has no passphrase.") from None
        except Exception as exc:  # wrong type, try the next
            last = exc
    raise SshError(f"The SSH key in the vault could not be read ({last}).")


class Session:
    def __init__(self, host: str, port: int, username: str, password: str | None = None,
                 private_key: str | None = None, passphrase: str | None = None):
        paramiko = _paramiko()
        if not username:
            raise SshError("The vault entry has no username.")
        if not password and not private_key:
            raise SshError("The vault entry has no password or SSH key.")

        class PinOnFirstUse(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                # Reached only for a host we have never seen; a *changed* key
                # is rejected by paramiko before any policy is consulted.
                with _known_hosts_lock:
                    client.get_host_keys().add(hostname, key.get_name(), key)
                    try:
                        client.save_host_keys(known_hosts_path())
                    except OSError:
                        pass

        self._client = client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except OSError:
            pass
        if os.path.exists(known_hosts_path()):
            try:
                client.load_host_keys(known_hosts_path())
            except OSError:
                pass
        client.set_missing_host_key_policy(PinOnFirstUse())

        pkey = _load_key(paramiko, private_key, passphrase) if private_key else None
        try:
            client.connect(
                host, port=port, username=username,
                password=None if pkey else password, pkey=pkey,
                timeout=CONNECT_TIMEOUT_SECONDS, banner_timeout=CONNECT_TIMEOUT_SECONDS,
                auth_timeout=CONNECT_TIMEOUT_SECONDS,
                # Use what the vault gave us and nothing else: not the agent,
                # not whatever keys happen to be in ~/.ssh.
                allow_agent=False, look_for_keys=False,
            )
        except paramiko.BadHostKeyException:
            raise SshError(
                f"The host key of {host} has changed since it was first seen. "
                f"If the device was replaced or reset, remove its line from "
                f"{known_hosts_path()} and try again."
            ) from None
        except paramiko.AuthenticationException:
            raise SshError("Sign-in was refused. Check the user and password in the vault.") from None
        except (paramiko.SSHException, socket.error, EOFError) as exc:
            raise SshError(f"Could not connect over SSH: {exc}") from None
        self._paramiko = paramiko
        self._shell = None
        self.prompt = ""

    def run(self, command: str) -> str:
        """Run one command as its own exec request and return its output."""
        try:
            _stdin, stdout, _stderr = self._client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
            return stdout.read().decode("utf-8", "replace")
        except (self._paramiko.SSHException, socket.error, EOFError) as exc:
            raise SshError(f"The SSH session dropped: {exc}") from None

    def _start_shell(self) -> None:
        if self._shell is not None:
            return
        self._shell = self._client.invoke_shell(width=511, height=1000)
        self._shell.settimeout(COMMAND_TIMEOUT_SECONDS)
        self.prompt = self._read_until_prompt().replace("\r", "").split("\n")[-1].strip()
        # Otherwise long output stops at "--More--" and waits for a key.
        self._shell.send("terminal length 0\n")
        self._read_until_prompt()

    def run_in_shell(self, command: str) -> str:
        """Run one command in the interactive session and return its output."""
        try:
            self._start_shell()
            self._shell.send(command + "\n")
            text = self._read_until_prompt()
        except (self._paramiko.SSHException, socket.error, EOFError) as exc:
            self._shell = None
            raise SshError(f"The SSH session dropped: {exc}") from None
        # Drop the echoed command at the top and the prompt at the bottom.
        lines = text.replace("\r", "").split("\n")
        self.prompt = lines[-1].strip()
        return "\n".join(lines[1:-1])

    def shell_prompt(self) -> str:
        """The device's current prompt, e.g. `Switch>` or `Switch#`."""
        try:
            self._start_shell()
        except (self._paramiko.SSHException, socket.error, EOFError) as exc:
            self._shell = None
            raise SshError(f"The SSH session dropped: {exc}") from None
        return self.prompt

    def enable(self, secret: str | None) -> bool:
        """Enter privileged mode on IOS. True if the prompt ends up at `#`.

        The enable secret is sent only in answer to the device's own
        `Password:` question, never as a command.
        """
        if self.shell_prompt().endswith("#"):
            return True
        if not secret:
            return False
        try:
            self._shell.send("enable\n")
            text = self._read_until_prompt(or_password=True)
            if _PASSWORD.search(text[-40:]):
                self._shell.send(secret + "\n")
                text = self._read_until_prompt(or_password=True)
                if _PASSWORD.search(text[-40:]):
                    # Wrong secret: IOS asks again. Send empty lines to back out.
                    for _ in range(3):
                        self._shell.send("\n")
                        text = self._read_until_prompt(or_password=True)
                        if not _PASSWORD.search(text[-40:]):
                            break
            self.prompt = text.replace("\r", "").split("\n")[-1].strip()
        except (self._paramiko.SSHException, socket.error, EOFError) as exc:
            self._shell = None
            raise SshError(f"The SSH session dropped: {exc}") from None
        return self.prompt.endswith("#")

    def _read_until_prompt(self, or_password: bool = False) -> str:
        received = ""
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            chunk = self._shell.recv(65535)
            if not chunk:
                raise EOFError("connection closed")
            received += chunk.decode("utf-8", "replace")
            if _PROMPT.search(received[-200:]):
                return received
            if or_password and _PASSWORD.search(received[-40:]):
                return received
        raise SshError("The device did not return to its prompt in time.")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
