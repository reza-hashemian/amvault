"""Session layer: the open vault, its entries, and lifecycle operations.

Storage is KeePass (.kdbx), so the same file opens in KeePassDX on Android and
Strongbox / KeePassium on iOS.  Everything that mutates entries goes through
here, keeping saving, dirty tracking, sync-conflict detection and auto-lock in
one place rather than duplicated across the GUI and CLI.
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
import time

from . import kdbx
from .kdbx import KdbxCredentialsError, KdbxError
from .models import Attachment, Category, Entry

DEFAULT_AUTOLOCK_SECONDS = 10 * 60
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MiB; keys and certs are far smaller


class VaultLocked(Exception):
    """Raised when an operation needs an unlocked vault and none is open."""


class SyncConflict(Exception):
    """Raised when the vault file changed on disk since we opened it.

    This is the normal outcome of editing on the phone and on the desktop
    between syncs.  Saving anyway would discard whichever side we did not load,
    so the caller has to decide.
    """

    def __init__(self, message: str, backup_path: str | None = None):
        super().__init__(message)
        self.backup_path = backup_path


class VaultManager:
    def __init__(self, path: str, autolock_seconds: int = DEFAULT_AUTOLOCK_SECONDS):
        self.path = path
        self.autolock_seconds = autolock_seconds
        self._kp = None
        self._entries: list[Entry] = []
        self._password: str | None = None
        self._keyfile: str | None = None
        self._last_activity = 0.0
        # Filesystem fingerprint at load time, used to spot outside edits.
        self._file_stamp: tuple | None = None
        self.dirty = False

    # --- state ----------------------------------------------------------
    @property
    def is_locked(self) -> bool:
        return self._kp is None

    @property
    def entries(self) -> list[Entry]:
        self._require_unlocked()
        return list(self._entries)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _require_unlocked(self) -> None:
        if self.is_locked:
            raise VaultLocked("The vault is locked.")

    def _stamp(self) -> tuple | None:
        """Fingerprint the file so outside modifications can be detected."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def file_changed_on_disk(self) -> bool:
        """True if something else wrote the vault since we loaded it."""
        if self._file_stamp is None:
            return False
        return self._stamp() != self._file_stamp

    # --- lifecycle ------------------------------------------------------
    def create(self, password: str, key_file_path: str | None = None) -> None:
        self._kp = kdbx.create(self.path, password, key_file_path)
        self._password, self._keyfile = password, key_file_path
        self._entries = []
        self._file_stamp = self._stamp()
        self.touch()
        self.dirty = False

    def unlock(self, password: str, key_file_path: str | None = None) -> None:
        self._kp = kdbx.open_database(self.path, password, key_file_path)
        self._password, self._keyfile = password, key_file_path
        self._entries = kdbx.load_entries(self._kp)
        self._file_stamp = self._stamp()
        self.touch()
        self.dirty = False

    def reload(self) -> None:
        """Re-read the file from disk, discarding unsaved changes.

        Used to adopt the other device's version after a sync conflict.
        """
        if self._password is None:
            raise VaultLocked("The vault is locked.")
        self.unlock(self._password, self._keyfile)

    def lock(self) -> None:
        """Drop the database handle and all decrypted data.

        Python cannot reliably zero immutable objects, so this is best-effort:
        references are dropped and left to the garbage collector.  A known
        limitation rather than a silent one.
        """
        self._kp = None
        self._entries = []
        self._password = None
        self._keyfile = None
        self._file_stamp = None
        self.dirty = False

    def touch(self) -> None:
        self._last_activity = time.time()

    def seconds_until_autolock(self) -> float:
        if self.is_locked or self.autolock_seconds <= 0:
            return float("inf")
        return max(0.0, self.autolock_seconds - (time.time() - self._last_activity))

    def check_autolock(self) -> bool:
        """Lock if idle too long. Returns True if it just locked."""
        if self.is_locked or self.autolock_seconds <= 0:
            return False
        if self.seconds_until_autolock() <= 0:
            self.lock()
            return True
        return False

    def save(self, force: bool = False) -> None:
        """Write the vault back to disk.

        Refuses when the file changed underneath us unless `force` is set, so a
        desktop save cannot silently erase edits synced from the phone.  A
        backup of the on-disk version is taken before forcing, so the other
        side's data is recoverable either way.
        """
        self._require_unlocked()

        if not force and self.file_changed_on_disk():
            backup = None
            try:
                backup = self._backup_conflicting_file()
            except OSError:
                pass
            raise SyncConflict(
                "The vault file changed on disk since you opened it — most "
                "likely it was edited on another device and synced.\n\n"
                "Saving now would overwrite those changes.",
                backup_path=backup,
            )

        kdbx.save_entries(self._kp, self._entries, self.path)
        self._file_stamp = self._stamp()
        self.dirty = False

    def _backup_conflicting_file(self) -> str:
        """Copy the on-disk (other device's) version aside before it is lost."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = f"{self.path}.conflict-{stamp}.kdbx"
        shutil.copy2(self.path, dest)
        return dest

    # --- entries --------------------------------------------------------
    def get(self, entry_id: str) -> Entry | None:
        self._require_unlocked()
        return next((e for e in self._entries if e.id == entry_id), None)

    def find(self, query: str = "", category: str | None = None) -> list[Entry]:
        self._require_unlocked()
        self.touch()
        results = [
            e for e in self._entries
            if e.matches(query) and (category is None or e.category == category)
        ]
        return sorted(results, key=lambda e: e.title.lower())

    def add(self, entry: Entry) -> Entry:
        self._require_unlocked()
        self._entries.append(entry)
        self.dirty = True
        self.touch()
        return entry

    def update(self, entry: Entry) -> None:
        self._require_unlocked()
        for i, existing in enumerate(self._entries):
            if existing.id == entry.id:
                entry.touch()
                self._entries[i] = entry
                self.dirty = True
                self.touch()
                return
        raise KeyError(f"No entry with id {entry.id}")

    def delete(self, entry_id: str) -> bool:
        self._require_unlocked()
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        removed = len(self._entries) < before
        if removed:
            self.dirty = True
            self.touch()
        return removed

    def all_tags(self) -> list[str]:
        self._require_unlocked()
        return sorted({t for e in self._entries for t in e.tags})

    def category_counts(self) -> dict:
        self._require_unlocked()
        counts = {c: 0 for c in Category.ALL}
        for e in self._entries:
            counts[e.category] = counts.get(e.category, 0) + 1
        return counts

    # --- attachments ----------------------------------------------------
    def attach_file(self, entry_id: str, file_path: str, name: str | None = None) -> Attachment:
        self._require_unlocked()
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(f"No entry with id {entry_id}")

        size = os.path.getsize(file_path)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"File is {size / 1024 / 1024:.1f} MiB; the limit is "
                f"{MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MiB. Large files make "
                f"every save slow and bloat the file you sync to your phone."
            )

        with open(file_path, "rb") as fh:
            data = fh.read()

        attachment = Attachment(name=name or os.path.basename(file_path), data=data)
        entry.attachments.append(attachment)
        entry.touch()
        self.dirty = True
        self.touch()
        return attachment

    def export_attachment(self, entry_id: str, name: str, dest_path: str) -> str:
        """Write an attachment back to disk with 0600 permissions.

        Used for SSH keys: the key leaves the vault only when asked for, and
        lands with owner-only permissions so ssh will accept it.
        """
        self._require_unlocked()
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(f"No entry with id {entry_id}")

        attachment = next((a for a in entry.attachments if a.name == name), None)
        if attachment is None:
            raise KeyError(f"No attachment named {name!r} on that entry")

        fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, attachment.data)
        finally:
            os.close(fd)
        self.touch()
        return dest_path

    def remove_attachment(self, entry_id: str, name: str) -> bool:
        self._require_unlocked()
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(f"No entry with id {entry_id}")
        before = len(entry.attachments)
        entry.attachments = [a for a in entry.attachments if a.name != name]
        removed = len(entry.attachments) < before
        if removed:
            entry.touch()
            self.dirty = True
            self.touch()
        return removed

    # --- maintenance ----------------------------------------------------
    def change_password(self, new_password: str, new_key_file_path: str | None = None) -> None:
        self._require_unlocked()
        self._kp.password = new_password or None
        self._kp.keyfile = new_key_file_path
        self._password = new_password
        self._keyfile = new_key_file_path
        self.save(force=True)

    def save_copy_beside(self) -> str:
        """Write the in-memory vault to a new file next to the original.

        Used when a conflict is detected with no user present to resolve it:
        `backup()` would only copy what is already on disk, which is precisely
        the version that does *not* contain the unsaved changes.
        """
        self._require_unlocked()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = f"{self.path}.mine-{stamp}.kdbx"
        kdbx.save_entries(self._kp, self._entries, dest)
        return dest

    def backup(self, dest_dir: str | None = None) -> str:
        """Copy the encrypted vault to a timestamped file.

        The copy is still encrypted, so backups can go anywhere -- an external
        drive, cloud storage -- without weakening anything.
        """
        if not self.exists():
            raise KdbxError("There is no vault file to back up yet.")
        dest_dir = dest_dir or os.path.dirname(os.path.abspath(self.path))
        os.makedirs(dest_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.basename(self.path)
        dest = os.path.join(dest_dir, f"{base}.{stamp}.bak")
        shutil.copy2(self.path, dest)
        return dest


# --- password generation ------------------------------------------------
AMBIGUOUS = "Il1O0`'\"|,.;:"


def generate_password(
    length: int = 24,
    use_upper: bool = True,
    use_lower: bool = True,
    use_digits: bool = True,
    use_symbols: bool = True,
    avoid_ambiguous: bool = False,
) -> str:
    """Generate a password using the OS CSPRNG.

    Guarantees at least one character from each selected class by rejection
    sampling rather than by placing them at fixed positions, which would leak
    structure.
    """
    pools = []
    if use_lower:
        pools.append(string.ascii_lowercase)
    if use_upper:
        pools.append(string.ascii_uppercase)
    if use_digits:
        pools.append(string.digits)
    if use_symbols:
        pools.append("!@#$%^&*()-_=+[]{}<>?/~")

    if not pools:
        raise ValueError("Select at least one character class.")

    if avoid_ambiguous:
        pools = ["".join(c for c in p if c not in AMBIGUOUS) for p in pools]
        pools = [p for p in pools if p]

    alphabet = "".join(pools)
    if length < len(pools):
        raise ValueError(
            f"Length {length} is too short to include all {len(pools)} character classes."
        )

    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(c in pool for c in candidate) for pool in pools):
            return candidate


DICE_WORDS_FALLBACK = [
    "anchor", "bridge", "canyon", "domain", "ember", "fabric", "granite", "harbor",
    "island", "jungle", "kernel", "lantern", "meadow", "nebula", "orbit", "prism",
    "quartz", "ridge", "summit", "timber", "umbrella", "vector", "willow", "zenith",
    "cipher", "beacon", "cobalt", "dynamo", "engine", "falcon", "gadget", "hollow",
]


def generate_passphrase(words: int = 5, separator: str = "-", capitalize: bool = False) -> str:
    """Generate a word-based passphrase, easier to type than a random string.

    Easier to type matters more now: this is the password you will re-enter on
    a phone keyboard every time you open the vault there.
    """
    wordlist = DICE_WORDS_FALLBACK
    for candidate_path in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
        if os.path.exists(candidate_path):
            try:
                with open(candidate_path, "r", encoding="utf-8", errors="ignore") as fh:
                    pool = [
                        w.strip().lower()
                        for w in fh
                        if 4 <= len(w.strip()) <= 9 and w.strip().isalpha()
                    ]
                if len(pool) >= 1000:
                    wordlist = sorted(set(pool))
                    break
            except OSError:
                pass

    chosen = [secrets.choice(wordlist) for _ in range(words)]
    if capitalize:
        chosen = [w.capitalize() for w in chosen]
    return separator.join(chosen)


def estimate_strength(password: str) -> dict:
    """Rough entropy estimate, for a strength meter.

    This measures resistance to brute force over the observed character classes
    only.  It cannot tell that "Password123!" is a common choice, so it will
    overrate predictable passwords -- a nudge, not a verdict.
    """
    if not password:
        return {"bits": 0.0, "label": "empty", "pool": 0}

    pool = 0
    if any(c.islower() for c in password):
        pool += 26
    if any(c.isupper() for c in password):
        pool += 26
    if any(c.isdigit() for c in password):
        pool += 10
    if any(not c.isalnum() for c in password):
        pool += 32

    import math
    bits = len(password) * math.log2(pool) if pool else 0.0

    if bits < 40:
        label = "weak"
    elif bits < 60:
        label = "fair"
    elif bits < 80:
        label = "strong"
    else:
        label = "very strong"

    return {"bits": round(bits, 1), "label": label, "pool": pool}
