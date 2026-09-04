"""Encrypted vault file: format, load, and save.

File layout (all JSON, one object)::

    {
      "format": "amvault",
      "version": 1,
      "kdf": {"salt": ..., "time_cost": ..., ...},   # cleartext, needed to derive
      "key_file_required": true|false,               # cleartext hint for the UI
      "payload": "<base64 of nonce||ciphertext||tag>"
    }

Only the KDF parameters and a "do I need a key file?" hint are in the clear.
Everything else -- titles, categories, tags, counts -- lives inside `payload`,
so the file leaks no metadata about what is stored or how much.

The header is bound to the ciphertext as GCM associated data, so editing the
KDF parameters (e.g. lowering the memory cost to make cracking cheaper) makes
the payload fail to authenticate rather than silently taking effect.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile

from .amv_crypto import DecryptionError, KdfParams, WeakParametersError, decrypt, derive_key, encrypt
from vault.models import Entry

FORMAT_NAME = "amvault"
FORMAT_VERSION = 1


class VaultFileError(Exception):
    pass


def _aad(kdf: KdfParams, key_file_required: bool) -> bytes:
    """Canonical bytes of the cleartext header, for GCM authentication."""
    return json.dumps(
        {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "kdf": kdf.to_dict(),
            "key_file_required": key_file_required,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def save_vault(
    path: str,
    entries: list[Entry],
    key: bytes,
    kdf: KdfParams,
    key_file_required: bool,
) -> None:
    """Encrypt and write the vault atomically.

    We write to a temp file in the same directory, fsync it, then rename over
    the target.  rename() is atomic within a filesystem, so a crash mid-save
    leaves either the old vault or the new one intact -- never a truncated
    file that would lose every secret at once.
    """
    plaintext = json.dumps(
        {"entries": [e.to_dict() for e in entries]},
        ensure_ascii=False,
    ).encode("utf-8")

    aad = _aad(kdf, key_file_required)
    payload = encrypt(key, plaintext, aad)

    document = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "kdf": kdf.to_dict(),
        "key_file_required": key_file_required,
        "payload": base64.b64encode(payload).decode("ascii"),
    }

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".vault-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            # Filesystems like exFAT/NTFS do not carry POSIX modes.  Not fatal
            # -- the contents are encrypted -- but worth knowing about.
            pass
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    # Also fsync the directory so the rename itself is durable, not just the
    # file contents.
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_header(path: str) -> dict:
    """Read the cleartext header without needing the password.

    The GUI uses this to know whether to show the key-file picker before
    asking for anything.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            document = json.load(fh)
    except FileNotFoundError:
        raise VaultFileError(f"No vault at {path}")
    except json.JSONDecodeError as exc:
        raise VaultFileError(f"{path} is not a valid vault file.") from exc

    if document.get("format") != FORMAT_NAME:
        raise VaultFileError(f"{path} is not an {FORMAT_NAME} vault.")
    if document.get("version", 0) > FORMAT_VERSION:
        raise VaultFileError(
            f"Vault was written by a newer version (v{document['version']}); "
            f"this build understands up to v{FORMAT_VERSION}."
        )
    return document


def load_vault(path: str, password: str, key_file_bytes: bytes | None = None):
    """Open a vault. Returns (entries, key, kdf_params, key_file_required)."""
    document = read_header(path)
    kdf = KdfParams.from_dict(document["kdf"])
    # Validate before deriving: the KDF runs on header-supplied numbers, ahead
    # of any authentication check, so weak values must be caught here.
    kdf.validate()
    key_file_required = bool(document.get("key_file_required", False))

    if key_file_required and not key_file_bytes:
        raise VaultFileError("This vault requires a key file.")

    key = derive_key(password, kdf, key_file_bytes)
    payload = base64.b64decode(document["payload"])
    aad = _aad(kdf, key_file_required)

    plaintext = decrypt(key, payload, aad)  # raises DecryptionError

    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Authentication passed, so this is a genuine format problem rather
        # than a wrong password.
        raise VaultFileError("Vault decrypted but its contents are unreadable.") from exc

    entries = [Entry.from_dict(e) for e in data.get("entries", [])]
    return entries, key, kdf, key_file_required


def create_vault(
    path: str,
    password: str,
    key_file_bytes: bytes | None = None,
    overwrite: bool = False,
):
    """Initialise a new empty vault. Returns (key, kdf_params)."""
    if os.path.exists(path) and not overwrite:
        raise VaultFileError(f"{path} already exists; refusing to overwrite it.")

    kdf = KdfParams.generate()
    key = derive_key(password, kdf, key_file_bytes)
    save_vault(path, [], key, kdf, key_file_required=bool(key_file_bytes))
    return key, kdf
