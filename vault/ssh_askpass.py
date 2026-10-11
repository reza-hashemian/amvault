"""Hold an entry's password for one ssh session to collect.

Typing a stored password into a terminal by hand is what a vault exists to
avoid, and the alternatives usually reached for are worse: `sshpass -p` puts
the password in the process list, and a temp file puts it on disk.

Instead the password stays in this process.  For one session a private local
endpoint is opened (a Unix socket in a 0700 directory; a named pipe on
Windows), ssh is told to run `amvault_askpass.py` when it wants a password,
and that helper collects it over the endpoint.  It is handed over exactly
once, to a peer that knows the session's random token, and the endpoint is
withdrawn afterwards or after a short wait, whichever comes first.

Needs OpenSSH 8.4 or newer for SSH_ASKPASS_REQUIRE; an older ssh ignores the
setting and simply asks in the terminal as before.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener

IS_WINDOWS = sys.platform == "win32"
# Long enough to read and answer a host-key question first.
OFFER_SECONDS = 180

HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "amvault_askpass.py")


class PasswordOffer:
    """One password, on offer to one ssh session."""

    def __init__(self, password: str, lifetime: float = OFFER_SECONDS):
        self._password = password
        self._token = secrets.token_bytes(32)
        self._done = threading.Event()
        self.collected = False

        base = None if IS_WINDOWS else (os.environ.get("XDG_RUNTIME_DIR") or None)
        if base and not os.path.isdir(base):
            base = None
        # mkdtemp makes it 0700; that, not the token, is what keeps other
        # users of this machine away from the socket.
        self._directory = tempfile.mkdtemp(prefix="amvault-askpass-", dir=base)
        if IS_WINDOWS:
            self._family = "AF_PIPE"
            self._address = rf"\\.\pipe\amvault-askpass-{secrets.token_hex(16)}"
        else:
            self._family = "AF_UNIX"
            self._address = os.path.join(self._directory, "s")
        self._program = self._write_launcher()

        self._listener = Listener(self._address, family=self._family, authkey=self._token)
        threading.Thread(target=self._serve, daemon=True).start()
        timer = threading.Timer(lifetime, self._expire)
        timer.daemon = True
        timer.start()

    def _write_launcher(self) -> str:
        """SSH_ASKPASS must name a single executable, so the helper gets a
        launcher that runs it with this same Python.  It holds no secret."""
        if IS_WINDOWS:
            path = os.path.join(self._directory, "askpass.cmd")
            body = f'@"{sys.executable}" "{HELPER}" %*\r\n'
        else:
            path = os.path.join(self._directory, "askpass")
            body = f'#!/bin/sh\nexec "{sys.executable}" "{HELPER}" "$@"\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, stat.S_IRWXU)
        return path

    def environment(self) -> dict:
        """What ssh's environment needs for this offer to be collected."""
        return {
            "SSH_ASKPASS": self._program,
            # `force`: use the helper even though there is a terminal to ask on.
            "SSH_ASKPASS_REQUIRE": "force",
            "AMVAULT_ASKPASS_ADDRESS": self._address,
            "AMVAULT_ASKPASS_FAMILY": self._family,
            "AMVAULT_ASKPASS_TOKEN": self._token.hex(),
        }

    def _serve(self) -> None:
        try:
            # accept() checks the token before returning a connection.
            connection = self._listener.accept()
        except Exception:
            self._withdraw()
            return
        try:
            if not self._done.is_set():
                connection.send_bytes(self._password.encode("utf-8"))
                self.collected = True
        except (OSError, ValueError):
            pass
        finally:
            connection.close()
            self._withdraw()

    def _expire(self) -> None:
        if self._done.is_set():
            return
        self._done.set()
        # A thread blocked in accept() is not woken by closing the listener,
        # so connect to it ourselves; _serve sees the flag and sends nothing.
        try:
            Client(self._address, family=self._family, authkey=self._token).close()
        except Exception:
            self._withdraw()

    def _withdraw(self) -> None:
        self._done.set()
        self._password = ""
        try:
            self._listener.close()
        except OSError:
            pass
        # The launcher is left for the life of the session's first minutes:
        # ssh runs it again for any later question (a retry, a passphrase),
        # and with the endpoint gone it then asks in the terminal.
        cleanup = threading.Timer(OFFER_SECONDS, shutil.rmtree, [self._directory], {"ignore_errors": True})
        cleanup.daemon = True
        cleanup.start()

    def close(self) -> None:
        """Take the offer back now, e.g. because the terminal failed to open."""
        self._expire()
        shutil.rmtree(self._directory, ignore_errors=True)
