"""KeePass (.kdbx) storage backend.

Using the KeePass format instead of a private one is what makes the vault
readable on a phone: KeePassDX (Android) and Strongbox / KeePassium (iOS) open
the same file this app writes, so one file can live on both ends of a sync.

Mapping between our model and the KeePass schema
------------------------------------------------
* Category      -> KeePass group. Mobile clients show these as folders.
* username /
  password /
  url           -> the standard KeePass fields, so other clients' autofill,
                   search and "copy password" buttons work as expected.
* other fields  -> custom string fields. Secret ones are marked *protected*,
                   which is what makes other clients mask them too.
* totp_secret   -> the de-facto `otp` field, as an otpauth:// URI, so phone
                   clients generate the same codes this app does.
* attachments   -> KeePass binaries.

Anything this app does not model (extra fields another client added) is
preserved on load and written back on save, so editing an entry here does not
silently drop data created on the phone.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile

from pykeepass import PyKeePass, create_database
from pykeepass.exceptions import CredentialsError, HeaderChecksumError, PayloadChecksumError

from .models import Attachment, Category, Entry
from .totp import TotpError, parse_uri

# Fields we store in KeePass's own columns rather than as custom properties.
STANDARD_FIELDS = {"username", "password", "url"}

# KeePass group name <-> our category id.
GROUP_NAMES = {
    Category.LOGIN: "Logins",
    Category.SSH_KEY: "SSH Keys",
    Category.API_KEY: "API Keys",
    Category.NETWORK: "Network Devices",
    Category.NOTE: "Secure Notes",
}
NAME_TO_CATEGORY = {v: k for k, v in GROUP_NAMES.items()}


class KdbxError(Exception):
    pass


def generate_key_file(path: str, size: int = 64) -> None:
    """Write a fresh random key file, readable only by the owner.

    KeePass accepts any file as a key file, hashing its bytes, so random data
    is both the simplest and the strongest choice.  Mobile clients read the
    same file, so this one can be copied to the phone alongside the vault --
    though doing so puts both factors on one device.
    """
    blob = secrets.token_bytes(size)
    # Create with 0600 from the start rather than chmod-ing after, so the bytes
    # are never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)


class KdbxCredentialsError(KdbxError):
    """Wrong password or key file."""


def _totp_uri(secret: str, title: str) -> str:
    """Wrap a bare base32 seed in an otpauth:// URI.

    Phone clients expect the URI form, and it is also what a QR code encodes,
    so storing it this way keeps the entry portable.
    """
    if secret.strip().lower().startswith("otpauth://"):
        return secret.strip()
    cleaned = secret.replace(" ", "").replace("-", "").strip().upper()
    label = title.replace(":", "_").replace("/", "_") or "amvault"
    return f"otpauth://totp/{label}?secret={cleaned}"


def _seed_from_uri(uri: str) -> str:
    """Reduce an otpauth:// URI back to the bare seed we store in fields."""
    if not uri:
        return ""
    if not uri.strip().lower().startswith("otpauth://"):
        return uri.strip()
    try:
        return parse_uri(uri)["secret"]
    except TotpError:
        # Keep the raw value rather than losing it; the TOTP widget will
        # report it as invalid, which is more useful than silently dropping it.
        return uri


def open_database(path: str, password: str, keyfile: str | None = None) -> PyKeePass:
    try:
        return PyKeePass(path, password=password or None, keyfile=keyfile)
    except CredentialsError as exc:
        raise KdbxCredentialsError(
            "Could not unlock: wrong password, wrong key file, or the file is "
            "not this vault."
        ) from exc
    except (HeaderChecksumError, PayloadChecksumError) as exc:
        raise KdbxError(f"The vault file appears to be corrupted: {exc}") from exc
    except FileNotFoundError as exc:
        raise KdbxError(f"No vault at {path}") from exc


def create(path: str, password: str, keyfile: str | None = None) -> PyKeePass:
    if os.path.exists(path):
        raise KdbxError(f"{path} already exists; refusing to overwrite it.")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    kp = create_database(path, password=password or None, keyfile=keyfile)
    # Pre-create the category groups so the folder structure is visible in
    # mobile clients even before anything is added.
    for name in GROUP_NAMES.values():
        kp.add_group(kp.root_group, name)
    kp.save()
    return kp


def entry_from_kp(kp_entry) -> Entry:
    """Convert a pykeepass entry into our model."""
    group_name = kp_entry.group.name if kp_entry.group is not None else ""
    category = NAME_TO_CATEGORY.get(group_name, Category.LOGIN)

    fields: dict = {}
    if kp_entry.username:
        fields["username"] = kp_entry.username
    if kp_entry.password:
        fields["password"] = kp_entry.password
    if kp_entry.url:
        fields["url"] = kp_entry.url

    for key, value in (kp_entry.custom_properties or {}).items():
        fields[key] = value

    if kp_entry.otp:
        fields["totp_secret"] = _seed_from_uri(kp_entry.otp)

    attachments = []
    for att in kp_entry.attachments:
        try:
            attachments.append(Attachment(name=att.filename, data=bytes(att.data)))
        except (KeyError, ValueError):
            # A binary referenced by the entry but missing from the database.
            # Skip it rather than failing the whole load.
            continue

    entry = Entry(
        title=kp_entry.title or "",
        category=category,
        fields=fields,
        tags=list(kp_entry.tags or []),
        notes=kp_entry.notes or "",
        attachments=attachments,
        id=str(kp_entry.uuid),
    )
    if kp_entry.ctime:
        entry.created = kp_entry.ctime.timestamp()
    if kp_entry.mtime:
        entry.modified = kp_entry.mtime.timestamp()
    return entry


def load_entries(kp: PyKeePass) -> list[Entry]:
    return [entry_from_kp(e) for e in kp.entries if not e.is_a_history_entry]


def _ensure_group(kp: PyKeePass, category: str):
    name = GROUP_NAMES.get(category, GROUP_NAMES[Category.LOGIN])
    found = kp.find_groups(name=name, first=True)
    return found if found is not None else kp.add_group(kp.root_group, name)


def _apply_entry(kp: PyKeePass, kp_entry, entry: Entry) -> None:
    """Write our model's values onto a pykeepass entry."""
    kp_entry.title = entry.title
    kp_entry.notes = entry.notes
    # pykeepass joins the list, so None is not an acceptable "no tags" value.
    kp_entry.tags = list(entry.tags) if entry.tags else []

    kp_entry.username = entry.fields.get("username", "") or ""
    kp_entry.password = entry.fields.get("password", "") or ""
    kp_entry.url = entry.fields.get("url", "") or ""

    if entry.fields.get("totp_secret"):
        kp_entry.otp = _totp_uri(entry.fields["totp_secret"], entry.title)
    elif kp_entry.otp:
        kp_entry.otp = ""

    # Rewrite custom properties: drop the ones no longer present, then set the
    # rest with the right protection flag.
    handled = STANDARD_FIELDS | {"totp_secret"}
    wanted = {k: v for k, v in entry.fields.items() if k not in handled}

    for key in list((kp_entry.custom_properties or {}).keys()):
        if key not in wanted:
            kp_entry.delete_custom_property(key)

    for key, value in wanted.items():
        kp_entry.set_custom_property(
            key, str(value), protect=entry.is_secret_field(key)
        )

    # Attachments: replace the set wholesale, since our model treats them as a
    # simple list and the counts here are small.
    existing = {a.filename: a for a in kp_entry.attachments}
    wanted_names = {a.name for a in entry.attachments}

    for name, att in existing.items():
        if name not in wanted_names:
            kp_entry.delete_attachment(att)

    for attachment in entry.attachments:
        if attachment.name in existing:
            continue
        binary_id = kp.add_binary(attachment.data)
        kp_entry.add_attachment(binary_id, attachment.name)


def save_entries(kp: PyKeePass, entries: list[Entry], path: str) -> None:
    """Sync our entry list into the database and write it out atomically.

    pykeepass writes in place, so we save to a temp copy first and rename over
    the original.  An interrupted save then leaves the previous vault intact
    rather than a half-written file.
    """
    by_uuid = {str(e.uuid): e for e in kp.entries if not e.is_a_history_entry}
    seen: set[str] = set()

    for entry in entries:
        kp_entry = by_uuid.get(entry.id)
        group = _ensure_group(kp, entry.category)

        if kp_entry is None:
            kp_entry = kp.add_entry(group, entry.title or "Untitled", "", "")
            # The id our model carries is only meaningful once the entry
            # exists in the database; adopt the real uuid.
            entry.id = str(kp_entry.uuid)
        elif kp_entry.group is not group:
            # Category changed -> move it to the matching group.
            kp.move_entry(kp_entry, group)

        _apply_entry(kp, kp_entry, entry)
        seen.add(entry.id)

    for uuid_str, kp_entry in by_uuid.items():
        if uuid_str not in seen:
            kp.delete_entry(kp_entry)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".vault-", suffix=".kdbx")
    os.close(fd)
    try:
        kp.save(tmp_path)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            # exFAT/NTFS carry no POSIX modes; the contents are encrypted anyway.
            pass
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
