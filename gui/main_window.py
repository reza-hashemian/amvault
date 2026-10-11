"""Main application window: sidebar, entry list, editor, and auto-lock."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from vault import portable
from vault.kdbx import KdbxError
from vault.manager import SyncConflict, VaultManager
from vault.models import Category, Entry

from . import theme
from .editor import CLIPBOARD_CLEAR_SECONDS, EntryEditor

AUTOLOCK_TICK_MS = 1000


class MainWindow(QMainWindow):
    locked = Signal()   # vault locked; the app should return to the unlock screen
    closed = Signal()   # window dismissed by the user; the app should quit
    nettool_requested = Signal()   # open the network tool for this vault

    def __init__(self, manager: VaultManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.current_category: str | None = None
        # An entry being composed but not yet added to the vault.
        self.pending_entry: Entry | None = None
        # Distinguishes "closing because we locked" from "user closed the app".
        self._locking = False
        self._clipboard_timer: QTimer | None = None
        self._clipboard_value: str | None = None

        self.setWindowTitle(f"amvault — {os.path.basename(manager.path)}")
        self.resize(1120, 720)
        self.setStyleSheet(theme.STYLESHEET)

        self._centre_on_screen()
        self._build_ui()
        self._build_menu()
        self._start_autolock_timer()
        self.refresh_list()

    def _centre_on_screen(self) -> None:
        """Open in the middle of the active screen rather than wherever the
        window manager decides, which on a wide monitor can be far off-centre."""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(
            available.center().x() - self.width() // 2,
            available.center().y() - self.height() // 2,
        )

    # --- UI construction ------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter)

        # --- left: categories ---
        sidebar = QWidget()
        # Wide enough for the longest label plus its count; "SSH Key /
        # Certificate (12)" was being clipped at 210px.
        sidebar.setMinimumWidth(232)
        sidebar.setMaximumWidth(260)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 16, 8, 12)
        sidebar_layout.setSpacing(6)

        brand = QLabel("amvault")
        brand.setObjectName("Heading")
        sidebar_layout.addWidget(brand)
        sidebar_layout.addSpacing(8)

        self.category_list = QListWidget()
        self.category_list.currentItemChanged.connect(self._on_category_selected)
        sidebar_layout.addWidget(self.category_list, 1)

        nettool_button = QPushButton("🌐  Network tool")
        nettool_button.setToolTip(
            "Open the network tool. It uses this unlocked vault, so it does "
            "not ask for the master password again.   (Ctrl+T)"
        )
        nettool_button.clicked.connect(self.nettool_requested.emit)
        sidebar_layout.addWidget(nettool_button)

        lock_button = QPushButton("🔒  Lock now")
        lock_button.clicked.connect(self.lock)
        sidebar_layout.addWidget(lock_button)

        self.autolock_label = QLabel("")
        self.autolock_label.setObjectName("SubHeading")
        self.autolock_label.setAlignment(Qt.AlignCenter)
        # Give it text immediately: an empty QLabel with a border reads as a
        # stray box until the first timer tick a second later.
        self._update_autolock_label(self.manager.seconds_until_autolock())
        sidebar_layout.addWidget(self.autolock_label)

        splitter.addWidget(sidebar)

        # --- middle: search + entry list ---
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(8, 16, 8, 12)
        middle_layout.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search…   (Ctrl+F)")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.refresh_list)
        middle_layout.addWidget(self.search_input)

        self.entry_list = QListWidget()
        self.entry_list.currentItemChanged.connect(self._on_entry_selected)
        middle_layout.addWidget(self.entry_list, 1)

        add_button = QPushButton("+  New entry")
        add_button.setObjectName("Primary")
        add_button.clicked.connect(self.new_entry)
        middle_layout.addWidget(add_button)

        splitter.addWidget(middle)

        # --- right: editor ---
        self.editor = EntryEditor(self.manager)
        self.editor.saved.connect(self._on_entry_saved)
        self.editor.deleted.connect(self._on_entry_deleted)
        self.editor.copy_requested.connect(self.copy_to_clipboard)
        self.editor.status.connect(self.show_status)
        splitter.addWidget(self.editor)

        splitter.setSizes([232, 300, 620])
        splitter.setStretchFactor(2, 1)

        self.status_bar = self.statusBar()
        self.show_status("Vault unlocked.")
        self._populate_categories()

    def _populate_categories(self) -> None:
        """Rebuild the sidebar, preserving the current selection."""
        previous = self.current_category
        self.category_list.blockSignals(True)
        self.category_list.clear()

        counts = self.manager.category_counts()
        total = sum(counts.values())

        all_item = QListWidgetItem(f"All items   ({total})")
        all_item.setData(Qt.UserRole, None)
        self.category_list.addItem(all_item)

        for category in Category.ALL:
            item = QListWidgetItem(f"{Category.LABELS[category]}   ({counts[category]})")
            item.setData(Qt.UserRole, category)
            item.setForeground(QColor(theme.CATEGORY_COLORS.get(category, theme.TEXT)))
            self.category_list.addItem(item)

        # Restore selection by value rather than by index.
        for i in range(self.category_list.count()):
            if self.category_list.item(i).data(Qt.UserRole) == previous:
                self.category_list.setCurrentRow(i)
                break
        else:
            self.category_list.setCurrentRow(0)

        self.category_list.blockSignals(False)

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        vault_menu = menubar.addMenu("&Vault")
        self._action(vault_menu, "&New entry", "Ctrl+N", self.new_entry)
        self._action(vault_menu, "&Save vault", "Ctrl+S", self.save_vault)
        vault_menu.addSeparator()
        self._action(vault_menu, "&Backup vault…", "Ctrl+B", self.backup_vault)
        self._action(vault_menu, "&Change master password…", None, self.change_password)
        vault_menu.addSeparator()
        self._action(vault_menu, "&Export all data to JSON…", None, self.export_json)
        self._action(vault_menu, "&Import data from JSON…", None, self.import_json)
        vault_menu.addSeparator()
        self._action(vault_menu, "&Lock", "Ctrl+L", self.lock)
        self._action(vault_menu, "&Quit", "Ctrl+Q", self.close)

        edit_menu = menubar.addMenu("&Edit")
        self._action(edit_menu, "&Find", "Ctrl+F", lambda: self.search_input.setFocus())
        self._action(edit_menu, "Copy &username", "Ctrl+Shift+U",
                     lambda: self._copy_field("username"))
        self._action(edit_menu, "Copy &password", "Ctrl+Shift+C",
                     lambda: self._copy_field("password"))

        tools_menu = menubar.addMenu("&Tools")
        self._action(tools_menu, "Open &network tool", "Ctrl+T",
                     self.nettool_requested.emit)

        help_menu = menubar.addMenu("&Help")
        self._action(help_menu, "&About", None, self.show_about)

    def _action(self, menu, text: str, shortcut: str | None, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    # --- auto-lock ------------------------------------------------------
    def _start_autolock_timer(self) -> None:
        self.autolock_timer = QTimer(self)
        self.autolock_timer.timeout.connect(self._tick_autolock)
        self.autolock_timer.start(AUTOLOCK_TICK_MS)

    def _tick_autolock(self) -> None:
        if self.manager.is_locked:
            return
        remaining = self.manager.seconds_until_autolock()
        if remaining <= 0:
            self.lock(auto=True)
            return
        self._update_autolock_label(remaining)

    def _update_autolock_label(self, remaining: float) -> None:
        if remaining == float("inf"):
            self.autolock_label.setText("auto-lock off")
            return
        minutes, seconds = divmod(int(remaining), 60)
        self.autolock_label.setText(f"locks in {minutes}:{seconds:02d}")

    def event(self, event):
        """Any user interaction counts as activity, deferring auto-lock."""
        if event.type() in (
            event.Type.KeyPress, event.Type.MouseButtonPress, event.Type.Wheel
        ):
            if not self.manager.is_locked:
                self.manager.touch()
        return super().event(event)

    def lock(self, auto: bool = False) -> None:
        if self.manager.dirty:
            if auto:
                # Do not lose work to a timer: save first, then lock.  If the
                # file changed elsewhere we cannot ask anything of an absent
                # user, so keep a copy rather than picking a winner.
                try:
                    self.manager.save()
                except SyncConflict:
                    # Nobody is present to choose a winner, so write our version
                    # to its own file rather than overwriting the synced one.
                    try:
                        rescue = self.manager.save_copy_beside()
                        self.show_status(
                            f"Sync conflict on auto-lock; your unsaved changes "
                            f"were written to {os.path.basename(rescue)}"
                        )
                    except Exception:
                        # Refuse to lock away edits we could not preserve.
                        QMessageBox.warning(
                            self, "Unsaved changes kept",
                            "The vault changed on another device and your "
                            "changes could not be saved automatically.\n\n"
                            "The vault was left unlocked so you can resolve it.",
                        )
                        return
                except Exception:
                    pass
            else:
                reply = QMessageBox.question(
                    self, "Unsaved changes",
                    "Save changes before locking?",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save,
                )
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.Save:
                    self.save_vault()

        self._clear_clipboard()
        self.manager.lock()
        self._locking = True
        self.close()
        self.locked.emit()

    # --- clipboard ------------------------------------------------------
    def copy_to_clipboard(self, value: str, label: str = "value") -> None:
        """Copy, then schedule a clear.

        The clipboard is readable by every process on the desktop, so a
        password left there is effectively public until overwritten.
        """
        if not value:
            self.show_status(f"Nothing to copy for {label}.")
            return

        QGuiApplication.clipboard().setText(value)
        self._clipboard_value = value

        if self._clipboard_timer:
            self._clipboard_timer.stop()
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setSingleShot(True)
        self._clipboard_timer.timeout.connect(self._clear_clipboard)
        self._clipboard_timer.start(CLIPBOARD_CLEAR_SECONDS * 1000)

        self.show_status(f"Copied {label} — clipboard clears in {CLIPBOARD_CLEAR_SECONDS}s.")

    def _clear_clipboard(self) -> None:
        """Clear only if the clipboard still holds what we put there.

        Otherwise we would wipe something the user copied afterwards.
        """
        clipboard = QGuiApplication.clipboard()
        if self._clipboard_value and clipboard.text() == self._clipboard_value:
            clipboard.clear()
        self._clipboard_value = None

    def _copy_field(self, name: str) -> None:
        entry = self._current_entry()
        if entry is None:
            self.show_status("Select an entry first.")
            return
        if name not in entry.fields:
            self.show_status(f"This entry has no {name} field.")
            return
        self.copy_to_clipboard(entry.fields[name], name)

    # --- list handling --------------------------------------------------
    def refresh_list(self) -> None:
        if self.manager.is_locked:
            return

        selected_id = None
        item = self.entry_list.currentItem()
        if item:
            selected_id = item.data(Qt.UserRole)

        self.entry_list.blockSignals(True)
        self.entry_list.clear()

        entries = self.manager.find(self.search_input.text(), self.current_category)
        for entry in entries:
            label = entry.title
            if entry.attachments:
                label += "  📎"
            if entry.fields.get("totp_secret"):
                label += "  ⏱"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry.id)
            item.setForeground(QColor(theme.CATEGORY_COLORS.get(entry.category, theme.TEXT)))
            if entry.tags:
                item.setToolTip(", ".join(entry.tags))
            self.entry_list.addItem(item)

        self.entry_list.blockSignals(False)

        # Restore the previous selection if it survived the filter.
        for i in range(self.entry_list.count()):
            if self.entry_list.item(i).data(Qt.UserRole) == selected_id:
                self.entry_list.setCurrentRow(i)
                return
        if not entries:
            self.editor.show_placeholder()

    def _current_entry(self) -> Entry | None:
        item = self.entry_list.currentItem()
        if item is None:
            return None
        return self.manager.get(item.data(Qt.UserRole))

    def _on_category_selected(self, current, _previous) -> None:
        if current is None:
            return
        self.current_category = current.data(Qt.UserRole)
        self.refresh_list()

    def _on_entry_selected(self, current, _previous) -> None:
        if current is None:
            # Do not clear the editor while a draft is being composed -- the
            # draft has no list row, so selection is legitimately empty.
            if self.pending_entry is None:
                self.editor.show_placeholder()
            return
        # Moving to a real entry abandons any unsaved draft.
        self.pending_entry = None
        entry = self.manager.get(current.data(Qt.UserRole))
        if entry:
            self.editor.is_draft = False
            self.editor.load(entry)

    # --- entry operations -----------------------------------------------
    def new_entry(self) -> None:
        """Open a blank entry in the editor without committing it yet.

        The draft is only added to the vault when the user presses Save.
        Adding it up front meant every stray click left a permanent "New entry"
        row behind, and saved it to disk.
        """
        category = self.current_category or Category.LOGIN
        entry = Entry(title="", category=category, fields={})
        # Pre-fill the template so the user sees the expected shape rather
        # than an empty form they must guess at.
        from vault.models import TEMPLATE_FIELDS
        for name in TEMPLATE_FIELDS.get(category, [])[:4]:
            entry.fields[name] = ""

        self.pending_entry = entry
        self.entry_list.setCurrentItem(None)
        self.editor.is_draft = True
        self.editor.load(entry)
        self.editor.title_input.setFocus()
        self.show_status("New entry — press Save to add it to the vault.")

    def _on_entry_saved(self, entry: Entry) -> None:
        if not entry.title.strip():
            QMessageBox.warning(
                self, "Name required",
                "Give the entry a name before saving it.",
            )
            self.editor.title_input.setFocus()
            return

        if self.pending_entry is not None and entry.id == self.pending_entry.id:
            # First save of a draft: this is when it joins the vault.
            self.manager.add(entry)
            self.pending_entry = None
            self.editor.is_draft = False
        else:
            self.manager.update(entry)

        self.save_vault()
        self._populate_categories()
        self.refresh_list()

        # Keep the just-saved entry selected so the user sees it land.
        for i in range(self.entry_list.count()):
            if self.entry_list.item(i).data(Qt.UserRole) == entry.id:
                self.entry_list.setCurrentRow(i)
                break

    def _on_entry_deleted(self, entry_id: str) -> None:
        if self.pending_entry is not None and entry_id == self.pending_entry.id:
            # Never added to the vault; discarding is all that is needed.
            self.pending_entry = None
            self.editor.show_placeholder()
            self.refresh_list()
            return
        self.manager.delete(entry_id)
        self.save_vault()
        self._populate_categories()
        self.refresh_list()
        self.editor.show_placeholder()

    def entries_changed(self, message: str = "") -> None:
        """Entries were added outside this window; save and show them."""
        self._populate_categories()
        self.refresh_list()
        if self.save_vault() and message:
            self.show_status(message)

    def save_vault(self) -> bool:
        """Write the vault, resolving a sync conflict with the user if needed.

        Returns True if the changes reached disk.
        """
        try:
            self.manager.save()
        except SyncConflict as conflict:
            return self._resolve_conflict(conflict)
        except (KdbxError, OSError) as exc:
            QMessageBox.critical(self, "Could not save", f"Writing the vault failed:\n{exc}")
            return False
        self.show_status("Saved.")
        return True

    def _resolve_conflict(self, conflict: SyncConflict) -> bool:
        """Ask what to do when the file changed underneath us.

        This is the everyday consequence of syncing: the phone edited the same
        vault.  Neither side is automatically right, so the user chooses -- and
        the version we are not keeping has already been copied aside.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Vault changed elsewhere")
        box.setText(str(conflict))
        if conflict.backup_path:
            box.setInformativeText(
                f"A copy of the version currently on disk was saved to:\n"
                f"{os.path.basename(conflict.backup_path)}\n\n"
                f"Keep my changes — overwrite the synced version.\n"
                f"Load from disk — discard my unsaved changes."
            )
        keep = box.addButton("Keep my changes", QMessageBox.AcceptRole)
        load = box.addButton("Load from disk", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is keep:
            try:
                self.manager.save(force=True)
            except (KdbxError, OSError) as exc:
                QMessageBox.critical(self, "Could not save", str(exc))
                return False
            self.show_status("Saved, overwriting the version from the other device.")
            return True

        if clicked is load:
            try:
                self.manager.reload()
            except (KdbxError, OSError) as exc:
                QMessageBox.critical(self, "Could not reload", str(exc))
                return False
            self.pending_entry = None
            self._populate_categories()
            self.refresh_list()
            self.editor.show_placeholder()
            self.show_status("Reloaded the version from the other device.")
            return True

        return False

    def backup_vault(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a folder for the backup")
        if not directory:
            return
        try:
            dest = self.manager.backup(directory)
        except (KdbxError, OSError) as exc:
            QMessageBox.warning(self, "Backup failed", str(exc))
            return
        QMessageBox.information(
            self, "Backup written",
            f"{dest}\n\nThe backup is still encrypted, so it is safe to keep on "
            f"external or cloud storage.",
        )

    def change_password(self) -> None:
        from .password_dialog import ChangePasswordDialog

        dialog = ChangePasswordDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            self.manager.change_password(dialog.new_password, dialog.key_file_path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not change password", str(exc))
            return
        QMessageBox.information(
            self, "Master password changed",
            "The vault was re-encrypted with a new salt and key.\n"
            "Any old backups still open with the old password — delete them "
            "if the old password may be compromised.",
        )

    # --- plaintext export / import --------------------------------------
    #
    # These exist for one job: moving into a brand-new vault when the old
    # password or key file may have leaked.  `Change master password` re-keys
    # the file in place and is the better answer most of the time; this pair is
    # for when you want a genuinely new file, which means the data has to leave
    # encryption for as long as it takes to carry it across.

    CONFIRM_WORD = "EXPORT"

    def export_json(self) -> None:
        entries = self.manager.entries
        if not entries:
            QMessageBox.information(self, "Nothing to export",
                                    "This vault has no entries yet.")
            return

        counts = portable.summarise(entries)
        attachment_note = (
            f"{counts['attachments']} attachments "
            f"({counts['attachment_bytes'] / 1024:.0f} KiB), including any SSH "
            f"private keys, are included in the file."
            if counts["attachments"] else
            "This vault has no attachments."
        )

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Export is not encrypted")
        box.setText(
            f"You are about to write {counts['entries']} entries to a plain "
            f"JSON file."
        )
        box.setInformativeText(
            f"Every password, token and key will be readable by anyone who "
            f"opens that file. {attachment_note}\n\n"
            f"Write it somewhere only you can reach, import it into the new "
            f"vault, then delete it."
        )
        proceed = box.addButton("I understand — choose a file", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not proceed:
            return

        # Typed confirmation as well as the button: this is the one action in
        # the app that undoes the encryption everything else provides.
        typed, ok = QInputDialog.getText(
            self, "Confirm plaintext export",
            f"Type {self.CONFIRM_WORD} to confirm:",
        )
        if not ok or typed.strip() != self.CONFIRM_WORD:
            self.show_status("Export cancelled.")
            return

        default_name = os.path.join(
            os.path.dirname(os.path.abspath(self.manager.path)),
            f"amvault-export-{time.strftime('%Y%m%d-%H%M%S')}.json",
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save plaintext export", default_name, "JSON files (*.json)"
        )
        if not path:
            return

        try:
            portable.write_export(entries, path)
        except (portable.ExportError, OSError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self, "Export written",
            f"{path}\n\n"
            f"{counts['entries']} entries and {counts['attachments']} "
            f"attachments, readable only by your user account.\n\n"
            f"This file is NOT encrypted. Delete it once the new vault is "
            f"working.",
        )
        self.show_status("Plaintext export written — delete it when you are done.")

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an amvault JSON export",
            os.path.dirname(os.path.abspath(self.manager.path)),
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return

        try:
            entries = portable.read_export(path)
        except portable.ImportError_ as exc:
            QMessageBox.critical(self, "Could not read that file", str(exc))
            return

        if not entries:
            QMessageBox.information(self, "Nothing to import",
                                    "That export contains no entries.")
            return

        counts = portable.summarise(entries)
        existing = len(self.manager.entries)

        # Imported entries always arrive as new ones -- say so plainly, because
        # importing into a vault that already holds the same data duplicates it
        # rather than merging.
        detail = (
            f"{counts['entries']} entries and {counts['attachments']} "
            f"attachments will be added."
        )
        if existing:
            detail += (
                f"\n\nThis vault already holds {existing} entries. They are "
                f"kept; the imported ones are added alongside, so anything "
                f"already here will appear twice."
            )

        reply = QMessageBox.question(
            self, "Import entries?", detail,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        for entry in entries:
            self.manager.add(entry)

        self._populate_categories()
        self.refresh_list()
        if not self.save_vault():
            # Left in memory but not on disk; the save path has already
            # explained why, and the entries are still here to retry with.
            self.show_status("Imported, but the vault could not be saved.")
            return

        QMessageBox.information(
            self, "Import complete",
            f"Added {counts['entries']} entries and {counts['attachments']} "
            f"attachments.\n\n"
            f"Check that they look right, then delete the JSON file — it is "
            f"not encrypted.",
        )

    def show_status(self, message: str) -> None:
        self.status_bar.showMessage(message, 6000)

    def show_about(self) -> None:
        QMessageBox.about(
            self, "About amvault",
            "<b>amvault</b><br><br>"
            "Offline encrypted vault for passwords, SSH keys, API tokens "
            "and network device credentials.<br><br>"
            "Argon2id key derivation · AES-256-GCM authenticated encryption<br>"
            "Optional key file as a true second factor<br><br>"
            "<i>For your highest-value secrets, consider a long-audited tool "
            "such as KeePassXC alongside this one.</i>",
        )

    def closeEvent(self, event) -> None:
        if self.manager.is_locked:
            event.accept()
            if not self._locking:
                # Locked and dismissed by the user, not by lock() itself.
                self.closed.emit()
            return
        if self.manager.dirty:
            reply = QMessageBox.question(
                self, "Unsaved changes", "Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                self.save_vault()

        self._clear_clipboard()
        self.manager.lock()
        event.accept()
        self.closed.emit()
