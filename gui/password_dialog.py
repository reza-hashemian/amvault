"""Dialog for changing the master password (and optionally the key file)."""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)

from vault.kdbx import generate_key_file
from vault.manager import estimate_strength

from . import theme


class ChangePasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.key_file_path: str | None = None
        self.new_password: str = ""

        self.setWindowTitle("Change master password")
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Change master password")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        note = QLabel(
            "The vault is re-encrypted with a fresh salt, so the old password "
            "stops working immediately. Existing backups keep their old password."
        )
        note.setObjectName("SubHeading")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.new_input = QLineEdit()
        self.new_input.setEchoMode(QLineEdit.Password)
        self.new_input.setPlaceholderText("New master password")
        self.new_input.textChanged.connect(self._on_changed)
        layout.addWidget(self.new_input)

        self.strength_bar = QProgressBar()
        self.strength_bar.setTextVisible(False)
        self.strength_bar.setMaximum(128)
        layout.addWidget(self.strength_bar)

        self.strength_label = QLabel("")
        self.strength_label.setObjectName("SubHeading")
        layout.addWidget(self.strength_label)

        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.setPlaceholderText("Confirm new master password")
        layout.addWidget(self.confirm_input)

        self.use_keyfile = QCheckBox("Require a key file from now on")
        self.use_keyfile.toggled.connect(self._sync)
        layout.addWidget(self.use_keyfile)

        row = QHBoxLayout()
        self.keyfile_display = QLineEdit()
        self.keyfile_display.setReadOnly(True)
        self.keyfile_display.setPlaceholderText("No key file selected")
        row.addWidget(self.keyfile_display, 1)

        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)

        self.generate_button = QPushButton("Generate")
        self.generate_button.clicked.connect(self._generate)
        row.addWidget(self.generate_button)
        layout.addLayout(row)

        self.error_label = QLabel("")
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync()

    def _sync(self) -> None:
        enabled = self.use_keyfile.isChecked()
        self.keyfile_display.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)
        self.generate_button.setEnabled(enabled)

    def _on_changed(self, text: str) -> None:
        strength = estimate_strength(text)
        self.strength_bar.setValue(min(int(strength["bits"]), 128))
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

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select key file")
        if path:
            self.key_file_path = path
            self.keyfile_display.setText(path)

    def _generate(self) -> None:
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
            self.error_label.setText(f"Could not write key file: {exc}")
            return
        self.key_file_path = path
        self.keyfile_display.setText(path)
        QMessageBox.information(
            self, "Key file created",
            f"Written to {path}\n\nBack it up separately — without it the vault "
            f"cannot be opened.",
        )

    def _accept(self) -> None:
        password = self.new_input.text()
        if not password:
            self.error_label.setText("Enter a new master password.")
            return
        if password != self.confirm_input.text():
            self.error_label.setText("The two passwords do not match.")
            return
        if self.use_keyfile.isChecked() and not self.key_file_path:
            self.error_label.setText("Select a key file, or turn the option off.")
            return
        if estimate_strength(password)["bits"] < 50:
            reply = QMessageBox.warning(
                self, "Weak master password",
                "That password is weak and protects every secret in the vault.\n\n"
                "Use it anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.new_password = password
        if not self.use_keyfile.isChecked():
            self.key_file_path = None
        self.accept()
