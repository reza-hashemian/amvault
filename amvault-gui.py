#!/usr/bin/env python3
"""amvault GUI entry point.

Shows the unlock window first; on success it opens the main window.  Locking
returns to the unlock window rather than quitting, so re-unlocking does not
mean restarting the app.

Lifecycle note: the app deliberately does not quit when the last window closes,
because locking closes the main window on purpose.  That makes quitting an
explicit decision, and every path that ends the session has to call quit() --
otherwise the event loop keeps running with nothing on screen and the terminal
appears to hang.
"""

from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Two jobs that are not "show the window", handled before Qt is imported so
# they start fast and work with no display.
#
# An installed copy has no separate Python to run helper scripts with, so the
# app is its own helper: ssh starts it with --askpass to collect a password
# (see vault/ssh_askpass.py).
if len(sys.argv) > 1 and sys.argv[1] == "--askpass":
    import amvault_askpass

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    sys.exit(amvault_askpass.main())

# Used by the release build to prove an installer's contents can actually
# start: everything the window needs is imported, and nothing is shown.
if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
    import PySide6.QtWidgets  # noqa: F401
    import amvault_keys  # noqa: F401
    import gui.main_window  # noqa: F401
    import gui.unlock  # noqa: F401
    import vault.broker  # noqa: F401
    import vault.manager  # noqa: F401

    sys.exit(0)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from gui.main_window import MainWindow
from gui.nettool import NetToolLauncher, NetToolNotFound
from gui.unlock import UnlockWindow


def _default_vault_path() -> str:
    """Where the vault lives by default.

    Beside the application rather than under ~/.local/share, so copying this
    directory to a USB stick takes the vault, the backups and the app together
    and it all runs from wherever it is plugged in.
    """
    if getattr(sys, "frozen", False):
        # Installed from a package: the program's own folder belongs to the
        # system and is replaced on every update, so the vault goes where the
        # platform keeps a user's application data.
        return os.path.join(_user_data_dir(), "vault.kdbx")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "data", "vault.kdbx")


def _user_data_dir() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or home, "amvault")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "amvault")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
                        "amvault")


DEFAULT_VAULT = _default_vault_path()


class Application:
    """Owns the unlock -> main -> lock -> unlock cycle.

    Windows are shown non-modally and driven by signals rather than nested
    exec() calls.  A nested loop would mean quit() ending only the innermost
    one, leaving the app alive with no visible window.
    """

    def __init__(self, vault_path: str):
        self.vault_path = vault_path
        self.main_window: MainWindow | None = None
        self.unlock_window: UnlockWindow | None = None
        # Outlives the main window, so the network tool stays connected across
        # a lock and resumes when the vault is unlocked again.
        self.nettool = NetToolLauncher(
            lambda: self.main_window.manager if self.main_window is not None else None,
            on_change=self._on_nettool_change,
        )

    def start(self) -> None:
        self.show_unlock()

    def show_unlock(self) -> None:
        window = UnlockWindow(self.vault_path)
        window.unlocked.connect(self.on_unlocked)
        # A rejected dialog means the user closed it instead of unlocking,
        # which is a request to quit.
        window.rejected.connect(self.quit)
        window.show()
        window.raise_()
        window.activateWindow()
        self.unlock_window = window

    def on_unlocked(self, manager) -> None:
        # Drop the unlock window only after the main window exists, so there is
        # never a moment with no window at all.
        self.main_window = MainWindow(manager)
        self.main_window.locked.connect(self.on_locked)
        self.main_window.closed.connect(self.quit)
        self.main_window.nettool_requested.connect(self.open_nettool)
        self.nettool.notify("unlocked")
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

        if self.unlock_window is not None:
            self.unlock_window.deleteLater()
            self.unlock_window = None

    def on_locked(self) -> None:
        """Locking returns to the unlock window rather than quitting."""
        self.main_window = None
        self.nettool.notify("locked")
        self.show_unlock()

    def _on_nettool_change(self) -> None:
        if self.main_window is not None:
            self.main_window.entries_changed("Device added from the network tool.")

    def open_nettool(self) -> None:
        window = self.main_window
        if window is None:
            return
        try:
            window.show_status(self.nettool.launch())
        except (NetToolNotFound, OSError) as exc:
            QMessageBox.warning(window, "Network tool", str(exc))

    def quit(self) -> None:
        self.nettool.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def main() -> int:
    vault_path = os.environ.get("AMVAULT_PATH", DEFAULT_VAULT)
    if len(sys.argv) > 1:
        vault_path = sys.argv[1]

    os.makedirs(os.path.dirname(vault_path), exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("amvault")
    # Locking closes the main window on purpose, so the last-window rule would
    # end the session every time the vault auto-locks.
    app.setQuitOnLastWindowClosed(False)

    # Qt installs its own handler and swallows Ctrl+C, which left the terminal
    # with no way out but killing it. Restore the default, and run a no-op timer
    # so the interpreter regularly gets a chance to notice the signal.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    keepalive = QTimer()
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)

    if os.environ.get("AMVAULT_DEBUG"):
        print(f"platform : {app.platformName()}", file=sys.stderr)
        print(f"vault    : {vault_path}", file=sys.stderr)
        for screen in app.screens():
            g = screen.geometry()
            print(f"screen   : {screen.name()} {g.width()}x{g.height()}+{g.x()}+{g.y()}",
                  file=sys.stderr)

    controller = Application(vault_path)
    controller.start()

    if os.environ.get("AMVAULT_DEBUG"):
        window = controller.unlock_window
        if window is not None:
            g = window.geometry()
            print(f"window   : {g.width()}x{g.height()} at ({g.x()},{g.y()}) "
                  f"visible={window.isVisible()}", file=sys.stderr)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
