"""Tests for the window lifecycle.

The app deliberately keeps running when its last window closes, because
locking closes the main window on purpose.  That makes every exit path an
explicit quit() call -- and a missed one leaves the process alive with nothing
on screen, which is indistinguishable from a hang.  These tests pin down which
paths must quit and which must not.
"""

import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from vault.manager import VaultManager

_app = QApplication.instance() or QApplication([])
_app.setQuitOnLastWindowClosed(False)


def load_gui_module():
    """Import amvault-gui.py, whose filename is not a valid module name."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "amvault_gui", os.path.join(here, "amvault-gui.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.gui = load_gui_module()
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "v.kdbx")
        self.quits = []
        self._real_quit = _app.quit
        _app.quit = lambda: self.quits.append(1)

    def tearDown(self):
        _app.quit = self._real_quit
        self.dir.cleanup()

    def pump(self, times=4):
        for _ in range(times):
            _app.processEvents()

    def make_vault(self):
        manager = VaultManager(self.path)
        manager.create("pw")
        return manager

    def test_closing_unlock_window_quits(self):
        app = self.gui.Application(self.path)
        app.start()
        self.pump()
        self.assertTrue(app.unlock_window.isVisible())

        app.unlock_window.reject()
        self.pump()
        self.assertTrue(self.quits, "closing the unlock window must quit the app")

    def test_closing_main_window_quits(self):
        app = self.gui.Application(self.path)
        app.on_unlocked(self.make_vault())
        self.pump()

        app.main_window.close()
        self.pump()
        self.assertTrue(self.quits, "closing the main window must quit the app")

    def test_locking_does_not_quit(self):
        app = self.gui.Application(self.path)
        manager = self.make_vault()
        app.on_unlocked(manager)
        self.pump()

        app.main_window.lock()
        self.pump()

        self.assertFalse(self.quits, "locking must not quit the app")
        self.assertTrue(manager.is_locked)
        self.assertIsNotNone(app.unlock_window)
        self.assertTrue(app.unlock_window.isVisible(),
                        "locking must return to the unlock screen")

    def test_quitting_after_a_lock_still_works(self):
        """The unlock window shown after locking must also be able to quit."""
        app = self.gui.Application(self.path)
        app.on_unlocked(self.make_vault())
        self.pump()
        app.main_window.lock()
        self.pump()

        app.unlock_window.reject()
        self.pump()
        self.assertTrue(self.quits)

    def test_unlock_window_released_after_unlocking(self):
        app = self.gui.Application(self.path)
        app.start()
        self.pump()
        self.assertIsNotNone(app.unlock_window)

        app.on_unlocked(self.make_vault())
        self.pump()
        self.assertIsNone(app.unlock_window,
                          "the unlock window should be released once unlocked")
        self.assertIsNotNone(app.main_window)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestVaultLocation(unittest.TestCase):
    def setUp(self):
        self.gui = load_gui_module()

    def test_from_source_the_vault_lives_beside_the_app(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(self.gui._default_vault_path(), os.path.join(here, "data", "vault.kdbx"))

    def test_an_installed_copy_keeps_it_in_the_users_data_folder(self):
        from unittest import mock

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/home/u/.local/share"}):
            self.assertEqual(self.gui._default_vault_path(),
                             "/home/u/.local/share/amvault/vault.kdbx")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("os.path.expanduser", return_value="/Users/u"):
            self.assertEqual(self.gui._default_vault_path(),
                             "/Users/u/Library/Application Support/amvault/vault.kdbx")
