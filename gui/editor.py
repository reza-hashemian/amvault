"""Entry editor panel: the right-hand side of the main window.

Secret fields render masked with a reveal toggle and a copy button.  Copy puts
the value on the clipboard and schedules a clear, so a password does not sit
in the clipboard indefinitely where any application could read it.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from vault import totp as totp_mod
from vault.ssh_connect import SshConnectError, connect, describe_target
from vault.manager import MAX_ATTACHMENT_BYTES, generate_password
from vault.models import Attachment, Category, Entry, TEMPLATE_FIELDS

from . import theme

CLIPBOARD_CLEAR_SECONDS = 30


class SecretField(QWidget):
    """A masked line edit with reveal and copy buttons."""

    copy_requested = Signal(str, str)  # value, label

    def __init__(self, name: str, value: str, is_secret: bool, parent=None):
        super().__init__(parent)
        self.name = name
        self.is_secret = is_secret

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.input = QLineEdit(value)
        self.input.setProperty("monospace", "true" if is_secret else "false")
        if is_secret:
            self.input.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.input, 1)

        if is_secret:
            self.reveal_button = QPushButton("👁")
            self.reveal_button.setObjectName("Flat")
            self.reveal_button.setFixedWidth(32)
            self.reveal_button.setToolTip("Show / hide")
            self.reveal_button.setCheckable(True)
            self.reveal_button.toggled.connect(self._toggle_reveal)
            layout.addWidget(self.reveal_button)

        self.copy_button = QPushButton("⧉")
        self.copy_button.setObjectName("Flat")
        self.copy_button.setFixedWidth(32)
        self.copy_button.setToolTip(
            f"Copy (clipboard clears after {CLIPBOARD_CLEAR_SECONDS}s)"
            if is_secret else "Copy"
        )
        self.copy_button.clicked.connect(
            lambda: self.copy_requested.emit(self.input.text(), self.name)
        )
        layout.addWidget(self.copy_button)

    def _toggle_reveal(self, shown: bool) -> None:
        self.input.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)

    def value(self) -> str:
        return self.input.text()


class TotpWidget(QWidget):
    """Live 2FA code with a countdown, refreshing as the period rolls over."""

    copy_requested = Signal(str, str)

    def __init__(self, secret: str, parent=None):
        super().__init__(parent)
        self.secret = secret

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.code_label = QLabel("------")
        self.code_label.setStyleSheet(
            f"font-family: 'JetBrains Mono', monospace; font-size: 20px; "
            f"font-weight: 600; letter-spacing: 3px; color: {theme.ACCENT};"
        )
        layout.addWidget(self.code_label)

        self.countdown = QLabel("")
        self.countdown.setObjectName("SubHeading")
        layout.addWidget(self.countdown)
        layout.addStretch(1)

        self.copy_button = QPushButton("Copy code")
        self.copy_button.setObjectName("Flat")
        self.copy_button.clicked.connect(
            lambda: self.copy_requested.emit(self.code_label.text().replace(" ", ""), "2FA code")
        )
        layout.addWidget(self.copy_button)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def refresh(self) -> None:
        try:
            code = totp_mod.generate(self.secret)
            remaining = totp_mod.seconds_remaining()
        except totp_mod.TotpError as exc:
            self.code_label.setText("invalid")
            self.code_label.setStyleSheet(f"color: {theme.DANGER}; font-size: 14px;")
            self.countdown.setText(str(exc))
            self.timer.stop()
            return

        # Group as 3+3 for readability when typing it in.
        self.code_label.setText(f"{code[:3]} {code[3:]}" if len(code) == 6 else code)
        self.countdown.setText(f"{remaining}s")
        self.countdown.setStyleSheet(
            f"color: {theme.DANGER};" if remaining <= 5 else f"color: {theme.TEXT_DIM};"
        )

    def stop(self) -> None:
        self.timer.stop()


class EntryEditor(QScrollArea):
    """Edits a single entry. Emits `saved` when the user commits changes."""

    saved = Signal(object)
    deleted = Signal(str)
    copy_requested = Signal(str, str)
    status = Signal(str)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.entry: Entry | None = None
        self.field_widgets: dict = {}
        self.totp_widget: TotpWidget | None = None
        # True while showing an entry that is not yet in the vault.
        self.is_draft = False

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)

        self.container = QWidget()
        self.setWidget(self.container)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(12)

        self.show_placeholder()

    # --- rendering ------------------------------------------------------
    def _clear(self) -> None:
        """Tear down the current form completely.

        deleteLater() alone is not enough here: it defers destruction to the
        event loop, so widgets from the previous entry are still parented and
        painted when the next entry renders on top of them.  Replacing the
        whole container widget detaches everything synchronously.
        """
        if self.totp_widget:
            self.totp_widget.stop()
            self.totp_widget = None
        self.field_widgets = {}

        old = self.container
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(12)
        # takeWidget() releases ownership before we hand over the replacement,
        # so the old tree can be destroyed without taking the new one with it.
        self.takeWidget()
        self.setWidget(self.container)
        old.setParent(None)
        old.deleteLater()

    def show_placeholder(self) -> None:
        self._clear()
        self.entry = None
        label = QLabel("Select an entry, or press Ctrl+N to add one.")
        label.setObjectName("SubHeading")
        label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(label)
        self.layout.addStretch(1)

    def load(self, entry: Entry) -> None:
        self._clear()
        self.entry = entry
        # Start every entry at the top. Without this the scroll offset from the
        # previously viewed entry carries over, which can leave the title field
        # above the viewport -- the entry looks nameless and unsaveable.
        self.verticalScrollBar().setValue(0)

        # --- title and category ---
        title_label = QLabel("Name")
        title_label.setObjectName("FieldLabel")
        self.layout.addWidget(title_label)

        self.title_input = QLineEdit(entry.title)
        self.title_input.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.title_input.setPlaceholderText("Name this entry")
        self.layout.addWidget(self.title_input)

        meta_row = QHBoxLayout()
        self.category_box = QComboBox()
        for category in Category.ALL:
            self.category_box.addItem(Category.LABELS[category], category)
        self.category_box.setCurrentIndex(Category.ALL.index(entry.category))
        self.category_box.currentIndexChanged.connect(self._on_category_changed)
        meta_row.addWidget(self.category_box)

        self.tags_input = QLineEdit(", ".join(entry.tags))
        self.tags_input.setPlaceholderText("tags, comma separated")
        meta_row.addWidget(self.tags_input, 1)
        wrapper = QWidget()
        wrapper.setLayout(meta_row)
        self.layout.addWidget(wrapper)

        # --- TOTP, if this entry has a seed ---
        if entry.fields.get("totp_secret"):
            totp_card = QFrame()
            totp_card.setObjectName("Card")
            card_layout = QVBoxLayout(totp_card)
            card_layout.setContentsMargins(14, 10, 14, 10)
            heading = QLabel("Two-factor code")
            heading.setObjectName("FieldLabel")
            card_layout.addWidget(heading)
            self.totp_widget = TotpWidget(entry.fields["totp_secret"])
            self.totp_widget.copy_requested.connect(self.copy_requested)
            card_layout.addWidget(self.totp_widget)
            self.layout.addWidget(totp_card)

        # --- connect bar ---
        # Only for entries that name a host: showing a dead button on a login
        # entry would just raise the question of why it does nothing.
        if entry.fields.get("host") or entry.fields.get("mgmt_ip"):
            self._add_connect_bar(entry)

        # --- fields ---
        fields_card = QFrame()
        fields_card.setObjectName("Card")
        self.fields_layout = QFormLayout(fields_card)
        self.fields_layout.setContentsMargins(14, 14, 14, 14)
        self.fields_layout.setSpacing(10)
        self.fields_layout.setLabelAlignment(Qt.AlignLeft)

        # Show the template order first so related fields sit together, then
        # anything the user added that is not in the template.
        template = TEMPLATE_FIELDS.get(entry.category, [])
        ordered = [f for f in template if f in entry.fields]
        ordered += [f for f in entry.fields if f not in template]

        for name in ordered:
            self._add_field_row(name, str(entry.fields[name]))

        self.layout.addWidget(fields_card)

        # --- add-field controls ---
        add_row = QHBoxLayout()
        self.new_field_box = QComboBox()
        self.new_field_box.setEditable(True)
        self.new_field_box.setPlaceholderText("field name")
        self._refresh_field_suggestions()
        add_row.addWidget(self.new_field_box, 1)

        add_button = QPushButton("Add field")
        add_button.clicked.connect(self._add_new_field)
        add_row.addWidget(add_button)

        gen_button = QPushButton("Generate password")
        gen_button.clicked.connect(self._generate_into_password)
        add_row.addWidget(gen_button)

        add_wrapper = QWidget()
        add_wrapper.setLayout(add_row)
        self.layout.addWidget(add_wrapper)

        # --- attachments ---
        attach_card = QFrame()
        attach_card.setObjectName("Card")
        attach_layout = QVBoxLayout(attach_card)
        attach_layout.setContentsMargins(14, 12, 14, 12)

        attach_header = QHBoxLayout()
        attach_title = QLabel("Attachments")
        attach_title.setObjectName("FieldLabel")
        attach_header.addWidget(attach_title)
        attach_header.addStretch(1)
        attach_button = QPushButton("Attach file…")
        attach_button.setObjectName("Flat")
        attach_button.clicked.connect(self._attach_file)
        attach_header.addWidget(attach_button)
        attach_layout.addLayout(attach_header)

        self.attachment_list = QListWidget()
        self.attachment_list.setMaximumHeight(120)
        self._refresh_attachments()
        attach_layout.addWidget(self.attachment_list)

        attach_actions = QHBoxLayout()
        save_as_button = QPushButton("Save to file…")
        save_as_button.clicked.connect(self._export_attachment)
        attach_actions.addWidget(save_as_button)
        remove_attachment_button = QPushButton("Remove")
        remove_attachment_button.setObjectName("Danger")
        remove_attachment_button.clicked.connect(self._remove_attachment)
        attach_actions.addWidget(remove_attachment_button)
        attach_actions.addStretch(1)
        attach_layout.addLayout(attach_actions)

        self.layout.addWidget(attach_card)

        # --- notes ---
        notes_label = QLabel("Notes")
        notes_label.setObjectName("FieldLabel")
        self.layout.addWidget(notes_label)
        self.notes_input = QTextEdit(entry.notes)
        self.notes_input.setMaximumHeight(110)
        self.layout.addWidget(self.notes_input)

        # --- actions ---
        actions = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self.commit)
        actions.addWidget(save_button)

        delete_button = QPushButton("Discard" if self.is_draft else "Delete entry")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete)
        actions.addWidget(delete_button)
        actions.addStretch(1)

        if self.is_draft:
            stamp = QLabel("not saved yet")
        else:
            modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.modified))
            stamp = QLabel(f"modified {modified}")
        stamp.setObjectName("SubHeading")
        actions.addWidget(stamp)

        actions_wrapper = QWidget()
        actions_wrapper.setLayout(actions)
        self.layout.addWidget(actions_wrapper)
        self.layout.addStretch(1)

    def _add_connect_bar(self, entry: Entry) -> None:
        """A one-click SSH session for entries that carry a host."""
        card = QFrame()
        card.setObjectName("Card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        target = describe_target(entry)
        label = QLabel(target)
        label.setStyleSheet(
            f"font-family: 'JetBrains Mono', monospace; font-size: 14px; "
            f"color: {theme.TEXT};"
        )
        layout.addWidget(label)
        layout.addStretch(1)

        # Say whether a key will be used, judged the same way the connect
        # handler judges it -- by content, not by filename.
        from amvault_keys import private_keys
        keys = private_keys(entry)
        if len(keys) == 1:
            hint_text, hint_color = f"با کلید {keys[0].name}", theme.SUCCESS
        elif len(keys) > 1:
            hint_text, hint_color = "چند کلید — یکی را نگه دار", theme.WARNING
        else:
            hint_text, hint_color = "بدون کلید", theme.TEXT_DIM

        hint = QLabel(hint_text)
        hint.setStyleSheet(f"color: {hint_color}; font-size: 12px;")
        layout.addWidget(hint)
        layout.addSpacing(4)

        button = QPushButton("اتصال  ⇥")
        button.setObjectName("Primary")
        button.setToolTip(f"ssh {target}")
        button.clicked.connect(lambda: self._connect_ssh(entry))
        layout.addWidget(button)

        self.layout.addWidget(card)

    def _connect_ssh(self, entry: Entry) -> None:
        """Open a terminal session, loading the entry's key first if it has one."""
        self._collect_into_entry()

        key_bytes = None
        # Pick the private key by content, the same way the CLI does, so a
        # public half or an unrelated attachment is never handed to ssh-add.
        from amvault_keys import pick_private_key
        attachment = pick_private_key(self.entry)
        if attachment is not None:
            key_bytes = attachment.data

        try:
            result = connect(
                self.entry,
                key_bytes=key_bytes,
                passphrase=self.entry.fields.get("passphrase") or None,
            )
        except SshConnectError as exc:
            QMessageBox.warning(self, "اتصال ممکن نشد", str(exc))
            return

        if result["via_agent"]:
            minutes = (result["lifetime"] or 0) // 60
            self.status.emit(
                f"در حال اتصال به {result['display']} — کلید تا {minutes} دقیقه "
                f"در ssh-agent می‌ماند."
            )
        elif result["key_loaded"]:
            # The agent was unreachable, so the key is a temporary file on
            # disk instead; connect() deletes it when the terminal exits.
            # Say so anyway -- the weaker guarantee is worth a mention.
            self.status.emit(
                f"در حال اتصال به {result['display']} — ssh-agent در دسترس نبود، "
                f"کلید موقتاً روی دیسک نوشته شد."
            )
        else:
            self.status.emit(f"در حال اتصال به {result['display']}")

    def _add_field_row(self, name: str, value: str) -> None:
        is_secret = self.entry.is_secret_field(name)
        widget = SecretField(name, value, is_secret)
        widget.copy_requested.connect(self.copy_requested)
        self.field_widgets[name] = widget

        label = QLabel(name.replace("_", " "))
        label.setObjectName("FieldLabel")
        self.fields_layout.addRow(label, widget)

    def _refresh_field_suggestions(self) -> None:
        self.new_field_box.clear()
        template = TEMPLATE_FIELDS.get(self.entry.category, [])
        self.new_field_box.addItems([f for f in template if f not in self.entry.fields])
        self.new_field_box.setCurrentText("")

    def _refresh_attachments(self) -> None:
        self.attachment_list.clear()
        for attachment in self.entry.attachments:
            size_kb = attachment.size / 1024
            item = QListWidgetItem(f"{attachment.name}   ({size_kb:.1f} KiB)")
            item.setData(Qt.UserRole, attachment.name)
            self.attachment_list.addItem(item)
        if not self.entry.attachments:
            placeholder = QListWidgetItem("No attachments")
            placeholder.setFlags(Qt.NoItemFlags)
            self.attachment_list.addItem(placeholder)

    # --- actions --------------------------------------------------------
    def _on_category_changed(self) -> None:
        """Changing category changes which fields count as secret.

        We re-render rather than trying to patch each widget, so masking always
        matches the new category.
        """
        if not self.entry:
            return
        self._collect_into_entry()
        self.entry.category = self.category_box.currentData()
        self.load(self.entry)

    def _add_new_field(self) -> None:
        name = self.new_field_box.currentText().strip().replace(" ", "_")
        if not name:
            return
        if name in self.field_widgets:
            self.status.emit(f"Field {name!r} already exists.")
            return
        self.entry.fields[name] = ""
        self._add_field_row(name, "")
        self._refresh_field_suggestions()

    def _generate_into_password(self) -> None:
        length, ok = QInputDialog.getInt(self, "Generate password", "Length:", 24, 8, 128)
        if not ok:
            return
        value = generate_password(length)

        # Put it in whichever secret field this category actually uses.
        target = next(
            (n for n in ("password", "passphrase", "key", "secret")
             if n in self.field_widgets),
            None,
        )
        if target is None:
            target = "password"
            self.entry.fields[target] = value
            self._add_field_row(target, value)
        else:
            self.field_widgets[target].input.setText(value)
        self.status.emit(f"Generated a {length}-character password into {target!r}.")

    def _attach_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach file")
        if not path:
            return
        self._collect_into_entry()

        # Attach to the entry object rather than going through the manager:
        # a draft is not in the vault yet, so a manager lookup by id would fail.
        # Either way the bytes are written when the entry is saved.
        try:
            size = os.path.getsize(path)
            if size > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"File is {size / 1024 / 1024:.1f} MiB; the limit is "
                    f"{MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MiB."
                )
            with open(path, "rb") as fh:
                data = fh.read()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not attach", str(exc))
            return

        name = os.path.basename(path)
        if any(a.name == name for a in self.entry.attachments):
            reply = QMessageBox.question(
                self, "Replace attachment",
                f"This entry already has a file called {name!r}. Replace it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self.entry.attachments = [
                a for a in self.entry.attachments if a.name != name
            ]

        self.entry.attachments.append(Attachment(name=name, data=data))
        self.entry.touch()
        self._refresh_attachments()
        self.status.emit(f"Attached {name} ({size} bytes). Press Save to store it.")

    def _selected_attachment(self) -> str | None:
        item = self.attachment_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _export_attachment(self) -> None:
        name = self._selected_attachment()
        if not name:
            self.status.emit("Select an attachment first.")
            return

        dest, _ = QFileDialog.getSaveFileName(self, "Save attachment as", name)
        if not dest:
            return
        attachment = next(
            (a for a in self.entry.attachments if a.name == name), None
        )
        if attachment is None:
            QMessageBox.warning(self, "Could not save", f"No attachment {name!r}.")
            return
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, attachment.data)
            finally:
                os.close(fd)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.status.emit(f"Wrote {dest} with owner-only permissions.")

    def _remove_attachment(self) -> None:
        name = self._selected_attachment()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Remove attachment",
            f"Remove {name!r} from this entry?\n"
            "It is only stored here — make sure you have a copy elsewhere.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.entry.attachments = [
            a for a in self.entry.attachments if a.name != name
        ]
        self.entry.touch()
        self._refresh_attachments()
        self.status.emit(f"Removed {name}. Press Save to persist.")

    def _collect_into_entry(self) -> None:
        """Pull widget values back into the entry object."""
        if not self.entry:
            return
        self.entry.title = self.title_input.text().strip()
        self.entry.category = self.category_box.currentData()
        self.entry.tags = [
            t.strip() for t in self.tags_input.text().split(",") if t.strip()
        ]
        self.entry.notes = self.notes_input.toPlainText()
        self.entry.fields = {
            name: widget.value() for name, widget in self.field_widgets.items()
        }

    def commit(self) -> None:
        if not self.entry:
            return
        self._collect_into_entry()
        self.saved.emit(self.entry)

    def _delete(self) -> None:
        if not self.entry:
            return
        if self.is_draft:
            # Nothing has been written yet; discarding needs no confirmation.
            self.deleted.emit(self.entry.id)
            return
        reply = QMessageBox.question(
            self, "Delete entry",
            f"Permanently delete {self.entry.title!r}?\n\n"
            f"This also deletes {len(self.entry.attachments)} attachment(s) "
            f"and cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.deleted.emit(self.entry.id)
