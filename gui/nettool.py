"""Launch the network tool and serve its credential requests.

The tool is a separate program with its own window.  This module finds it,
starts it, and answers what it asks through a `BrokerSession` -- which is what
lets it open without asking for the master password again.

The session belongs to the application, not to the main window: locking closes
the main window, and the tool should survive that and pick up again when the
vault is unlocked.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import Future

from PySide6.QtCore import QObject, Signal

from vault.broker import BrokerSession, handle_request

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY_NAME = "nettool.exe" if sys.platform == "win32" else "nettool"

# Where each platform's installer puts the tool.
_INSTALL_LOCATIONS = (
    r"%LOCALAPPDATA%\nettool\nettool.exe",
    r"%ProgramFiles%\nettool\nettool.exe",
    "/Applications/nettool.app/Contents/MacOS/nettool",
    "~/Applications/nettool.app/Contents/MacOS/nettool",
)

# Requests arrive on the session's thread but touch the vault, which the GUI
# also mutates.  They are run on the GUI thread; this bounds the wait for it.
MAIN_THREAD_TIMEOUT_SECONDS = 15


class NetToolNotFound(Exception):
    pass


def find_binary() -> str:
    """Locate the network tool's executable.

    `AMVAULT_NETTOOL` wins.  Otherwise look in this repository's `nettool/`
    folder (running from source: a build there, or a binary copied in for the
    portable layout), then in sibling directories (where the tool lived when
    it was a separate project), then for a copy put in place by its installer.
    """
    override = os.environ.get("AMVAULT_NETTOOL")
    if override:
        if os.path.isfile(override):
            return override
        raise NetToolNotFound(f"AMVAULT_NETTOOL points to a file that does not exist:\n{override}")

    parent = os.path.dirname(APP_ROOT)
    roots = [os.path.join(APP_ROOT, "nettool")]
    for pattern in ("*net-tool*", "*nettool*"):
        # glob.escape: the parent path may itself contain [brackets].
        roots += sorted(glob.glob(os.path.join(glob.escape(parent), pattern)))

    for root in roots:
        for relative in (BINARY_NAME, os.path.join("src-tauri", "target", "release", BINARY_NAME)):
            candidate = os.path.join(root, relative)
            if os.path.isfile(candidate):
                return candidate

    # Lastly a copy put in place by its installer.
    installed = shutil.which("nettool")
    if installed:
        return installed
    for candidate in _INSTALL_LOCATIONS:
        candidate = os.path.expandvars(os.path.expanduser(candidate))
        if os.path.isfile(candidate):
            return candidate

    raise NetToolNotFound(
        "The network tool was not found.\n\n"
        "Install it from its releases page, or build it: in the net-tool "
        "folder run\n"
        "    npm install\n"
        "    npm run tauri build -- --no-bundle\n\n"
        "You can also set AMVAULT_NETTOOL to the path of its executable."
    )


class NetToolLauncher(QObject):
    """Owns the running tool's session and answers it on the GUI thread."""

    # Carries (callable, Future) from a session thread to the GUI thread.
    _call_on_main = Signal(object, object)

    def __init__(self, manager_provider, on_change=None, parent=None):
        """`manager_provider` returns the open VaultManager, or None if locked.

        `on_change` is called on the GUI thread after the tool has added an
        entry, to save the vault and redraw the list.
        """
        super().__init__(parent)
        self._manager_provider = manager_provider
        self._on_change = on_change
        self._session: BrokerSession | None = None
        self._process: subprocess.Popen | None = None
        self._call_on_main.connect(self._run_on_main)

    @property
    def is_running(self) -> bool:
        return (
            self._session is not None
            and self._session.is_alive
            and self._process is not None
            and self._process.poll() is None
        )

    def launch(self) -> str:
        """Open the tool, or bring the open one forward.  Returns a status line.

        Raises NetToolNotFound or OSError if it cannot be started.
        """
        if self.is_running:
            self._session.notify("focus")
            return "Network tool is already open."

        binary = find_binary()
        self.shutdown()
        session = BrokerSession(self._handle, on_closed=self._on_session_closed)
        handshake = {"broker": session.handshake()}
        if not getattr(sys, "frozen", False):
            # Offered to a from-source tool as a Python to run its engine
            # with. An installed amvault is not a Python interpreter.
            handshake["python"] = sys.executable

        env = dict(os.environ, NETTOOL_HANDSHAKE="stdin")
        try:
            process = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                # A windowed Python (pythonw) has no usable stderr to inherit.
                stdout=subprocess.DEVNULL,
                stderr=None if sys.stderr is not None else subprocess.DEVNULL,
                env=env,
                cwd=os.path.dirname(binary),
            )
            process.stdin.write((json.dumps(handshake) + "\n").encode("utf-8"))
            process.stdin.close()
        except OSError:
            session.close()
            raise

        self._session, self._process = session, process
        return "Network tool opened."

    def notify(self, event: str) -> None:
        if self._session is not None and self._session.is_alive:
            self._session.notify(event)

    def shutdown(self) -> None:
        """End the session.  The tool notices and shows that it is disconnected."""
        session, self._session = self._session, None
        if session is not None:
            session.close()

    # --- request handling -----------------------------------------------
    def _on_session_closed(self, session: BrokerSession) -> None:
        if self._session is session:
            self._session = None

    def _handle(self, request: dict) -> dict:
        """Called on the session thread; does the work on the GUI thread."""
        future: Future = Future()
        self._call_on_main.emit(
            lambda: handle_request(self._manager_provider(), request, self._on_change), future
        )
        return future.result(timeout=MAIN_THREAD_TIMEOUT_SECONDS)

    def _run_on_main(self, job, future: Future) -> None:
        try:
            future.set_result(job())
        except BaseException as exc:
            future.set_exception(exc)
