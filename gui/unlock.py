"""Unlock / create-vault window.

Key derivation is deliberately slow (~0.5s), which is long enough to freeze a
single-threaded UI and make the app look hung.  So the Argon2 call runs on a
worker thread and the window shows a progress indicator meanwhile.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from vault.kdbx import KdbxCredentialsError, KdbxError, generate_key_file
from vault.manager import VaultManager, estimate_strength

from . import theme


class UnlockWorker(QThread):
    """Runs create/unlock off the UI thread."""

    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, manager: VaultManager, password: str,
                 key_file: str | None, create: bool):
        super().__init__()
        self.manager = manager
        self.password = password
        self.key_file = key_file
        self.create = create

    def run(self) -> None:
        try:
            if self.create:
                self.manager.create(self.password, self.key_file)
            else:
                self.manager.unlock(self.password, self.key_file)
            self.succeeded.emit()
        except KdbxCredentialsError:
            self.failed.emit(
                "Wrong master password or key file.\n"
                "(The vault cannot tell you which one was wrong.)"
            )
        except (KdbxError, ValueError, OSError) as exc:
            self.failed.emit(str(exc))


class UnlockWindow(QDialog):
    unlocked = Signal(object)  # emits the unlocked VaultManager

    DIALOG_WIDTH = 440
    # Dialog width minus the outer 32px margins and the card's 14px padding.
    CONTENT_WIDTH = DIALOG_WIDTH - (32 * 2) - (14 * 2)

    def __init__(self, vault_path: str, parent=None):
        super().__init__(parent)
        self.vault_path = vault_path
        self.manager = VaultManager(vault_path)
        self.is_new = not self.manager.exists()
        self.key_file_path: str | None = None
        self.worker: UnlockWorker | None = None

        self.setWindowTitle("amvault — باز کردن گاوصندوق")
        self.setFixedWidth(self.DIALOG_WIDTH)
        self.setStyleSheet(theme.STYLESHEET)
        self._build()
        self._sync_state()
        self._centre_on_screen()

    def _centre_on_screen(self) -> None:
        """Place the window in the middle of the active screen.

        Left to the window manager, a small dialog can land off to one side of
        a wide monitor or behind the terminal it was started from.
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        size = self.sizeHint()
        self.move(
            available.center().x() - size.width() // 2,
            available.center().y() - size.height() // 2,
        )

    def showEvent(self, event):
        """Come to the front on first show, without staying always-on-top."""
        super().showEvent(event)
        if not getattr(self, "_raised_once", False):
            self._raised_once = True
            self._centre_on_screen()
            self.raise_()
            self.activateWindow()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        title = QLabel("Create your vault" if self.is_new else "Unlock vault")
        title.setObjectName("Heading")
        layout.addWidget(title)

        subtitle = QLabel(
            "Choose a strong master password. It is the only thing protecting "
            "everything inside, and it cannot be recovered."
            if self.is_new else
            os.path.basename(self.vault_path)
        )
        subtitle.setObjectName("SubHeading")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("Master password")
        self.password_input.returnPressed.connect(self._submit)
        self.password_input.textChanged.connect(self._on_password_changed)
        layout.addWidget(self.password_input)

        # Strength meter, only meaningful while choosing a new password.
        self.strength_bar = QProgressBar()
        self.strength_bar.setTextVisible(False)
        self.strength_bar.setMaximum(128)
        self.strength_label = QLabel("")
        self.strength_label.setObjectName("SubHeading")
        if self.is_new:
            layout.addWidget(self.strength_bar)
            layout.addWidget(self.strength_label)

        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.setPlaceholderText("Confirm master password")
        self.confirm_input.returnPressed.connect(self._submit)
        if self.is_new:
            layout.addWidget(self.confirm_input)

        # --- key file (second factor) ---
        keyfile_frame = QFrame()
        keyfile_frame.setObjectName("Card")
        kf_layout = QVBoxLayout(keyfile_frame)
        kf_layout.setContentsMargins(14, 12, 14, 12)
        kf_layout.setSpacing(8)
        # Without this the frame keeps its own sizeHint and clips the wrapped
        # label inside it, however tall that label says it needs to be.
        kf_layout.setSizeConstraint(QVBoxLayout.SetMinimumSize)

        self.use_keyfile = QCheckBox("Also require a key file (two-factor)")
        self.use_keyfile.toggled.connect(self._sync_state)
        kf_layout.addWidget(self.use_keyfile)

        explain = QLabel(
            "A second secret, kept on a USB stick. Its bytes become part of "
            "the encryption key, so the vault cannot be opened without it."
        )
        explain.setObjectName("SubHeading")
        explain.setWordWrap(True)
        # A word-wrapped QLabel reports a single-line sizeHint, which the card
        # then honours and clips.  Ask the label how tall its wrap really is at
        # the known content width and hold it there.
        explain.setMinimumHeight(explain.heightForWidth(self.CONTENT_WIDTH))
        explain.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        kf_layout.addWidget(explain)

        row = QHBoxLayout()
        self.keyfile_display = QLineEdit()
        self.keyfile_display.setReadOnly(True)
        self.keyfile_display.setPlaceholderText("No key file selected")
        row.addWidget(self.keyfile_display, 1)

        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse_key_file)
        row.addWidget(self.browse_button)

        self.generate_button = QPushButton("Generate")
        self.generate_button.clicked.connect(self._generate_key_file)
        row.addWidget(self.generate_button)
        kf_layout.addLayout(row)

        self.keyfile_frame = keyfile_frame
        layout.addWidget(keyfile_frame)

        # Deliberately outside the card: this is the consequence people most
        # need to read, and the card's fixed height was swallowing it.
        self.keyfile_warning = QLabel(
            "⚠  Back the key file up separately — losing it loses the vault."
        )
        self.keyfile_warning.setWordWrap(True)
        self.keyfile_warning.setStyleSheet(f"color: {theme.WARNING}; font-size: 12px;")
        self.keyfile_warning.setMinimumHeight(
            self.keyfile_warning.heightForWidth(self.CONTENT_WIDTH)
        )
        layout.addWidget(self.keyfile_warning)

        self.message = QLabel("")
        self.message.setObjectName("Error")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)  # indeterminate
        self.busy.setTextVisible(False)
        self.busy.hide()
        layout.addWidget(self.busy)

        self.submit_button = QPushButton("Create vault" if self.is_new else "Unlock")
        self.submit_button.setObjectName("Primary")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self._submit)
        layout.addWidget(self.submit_button)

        hint = QLabel(f"Vault file: {self.vault_path}")
        hint.setObjectName("SubHeading")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _sync_state(self) -> None:
        """Show the key-file controls only when they are relevant."""
        if self.is_new:
            enabled = self.use_keyfile.isChecked()
            self.keyfile_display.setEnabled(enabled)
            self.browse_button.setEnabled(enabled)
            self.generate_button.setEnabled(enabled)
            self.keyfile_warning.setVisible(enabled)
        else:
            # KDBX offers no way to ask whether a key file is required without
            # attempting to open the file, so the option stays available and
            # the user tells us.
            enabled = self.use_keyfile.isChecked()
            self.use_keyfile.setText("This vault uses a key file")
            self.generate_button.hide()
            self.keyfile_display.setEnabled(enabled)
            self.browse_button.setEnabled(enabled)
            self.keyfile_warning.hide()

    def _on_password_changed(self, text: str) -> None:
        if not self.is_new:
            return
        strength = estimate_strength(text)
        bits = min(int(strength["bits"]), 128)
        self.strength_bar.setValue(bits)

        color = {
            "empty": theme.TEXT_DIM,
            "weak": theme.DANGER,
            "fair": theme.WARNING,
            "strong": theme.SUCCESS,
            "very strong": theme.SUCCESS,
        }[strength["label"]]
        self.strength_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}"
        )
        self.strength_label.setText(
            "" if not text else f"{strength['label']} · ~{strength['bits']} bits"
        )

    def _browse_key_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select key file")
        if path:
            self.key_file_path = path
            self.keyfile_display.setText(path)

    def _generate_key_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save new key file", "vault.key", "Key files (*.key);;All files (*)"
        )
        if not path:
            return
        try:
            if os.path.exists(path):
                os.unlink(path)
            generate_key_file(path)
        except OSError as exc:
            self._show_error(f"Could not write key file: {exc}")
            return

        self.key_file_path = path
        self.keyfile_display.setText(path)
        QMessageBox.information(
            self,
            "Key file created",
            f"A random key file was written to:\n{path}\n\n"
            "Copy it somewhere safe — a USB stick, or another machine.\n"
            "If you lose it, this vault can never be opened again.",
        )

    def _show_error(self, text: str) -> None:
        self.message.setObjectName("Error")
        self.message.setStyleSheet(f"color: {theme.DANGER};")
        self.message.setText(text)

    def _set_busy(self, busy: bool) -> None:
        self.busy.setVisible(busy)
        self.submit_button.setEnabled(not busy)
        self.password_input.setEnabled(not busy)
        self.confirm_input.setEnabled(not busy)
        self.submit_button.setText(
            "Deriving key…" if busy else ("Create vault" if self.is_new else "Unlock")
        )

    def _submit(self) -> None:
        if self.worker and self.worker.isRunning():
            return

        password = self.password_input.text()
        if not password:
            self._show_error("Enter a master password.")
            return

        if self.is_new:
            if password != self.confirm_input.text():
                self._show_error("The two passwords do not match.")
                return
            if estimate_strength(password)["bits"] < 50:
                reply = QMessageBox.warning(
                    self,
                    "Weak master password",
                    "That master password is weak. It protects every secret in "
                    "the vault, and there is no way to recover it.\n\n"
                    "Use it anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return

        key_file = self.key_file_path if self.use_keyfile.isChecked() else None
        if self.use_keyfile.isChecked() and not key_file:
            self._show_error("Select a key file, or turn the option off.")
            return

        self.message.setText("")
        self._set_busy(True)

        self.worker = UnlockWorker(self.manager, password, key_file, self.is_new)
        self.worker.succeeded.connect(self._on_success)
        self.worker.failed.connect(self._on_failure)
        self.worker.start()

    def _on_success(self) -> None:
        self._set_busy(False)
        # Clear the password from the widget now that it is no longer needed.
        self.password_input.clear()
        self.confirm_input.clear()
        self.unlocked.emit(self.manager)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(message)
        self.password_input.selectAll()
        self.password_input.setFocus()
