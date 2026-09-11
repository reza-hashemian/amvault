"""Plaintext JSON export and import — the escape hatch from one vault file.

The point of this module is key rotation.  If the key file or master password
leaks, changing the password re-encrypts the *same* file; that is usually
enough, but it keeps the old file's identity, history and any copies already
synced elsewhere.  Starting from a genuinely new vault means moving the data
across, and the only format both ends can agree on without the old key is
plaintext.

Attachments travel inside the JSON as base64, because `Entry.to_dict()`
already encodes them that way.  One exported file is therefore self-contained:
SSH private keys and certificates come back on import without any companion
files to keep track of.

That self-containment is exactly why the file is dangerous.  Everything the
vault protects is readable in it.  Callers must make that explicit -- the CLI
demands a flag, the GUI demands a typed confirmation -- and the file is
written 0600 so it is at least not readable by other users on the machine.
"""

from __future__ import annotations

import json
import os
import time

from .models import Attachment, Entry, new_id

# Bumped only if the shape changes incompatibly.  Import accepts any version it
# understands and refuses the rest, rather than guessing at a newer layout.
FORMAT = "amvault-export"
VERSION = 1
SUPPORTED_VERSIONS = {1}


class ExportError(Exception):
    pass


class ImportError_(Exception):
    """Raised when a file is not a usable export.

    Named with a trailing underscore to avoid shadowing the builtin
    ``ImportError``, which would be a confusing thing to catch by accident.
    """


def build_payload(entries: list[Entry]) -> dict:
    """Assemble the export document.

    The header exists so import can reject a file that is merely JSON, and so a
    human opening the file later can tell what produced it.
    """
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": time.time(),
        "exported_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_count": len(entries),
        "warning": (
            "PLAINTEXT. Every password, key and attachment in this file is "
            "readable by anyone who opens it. Delete it once you have "
            "imported it into the new vault."
        ),
        "entries": [e.to_dict() for e in entries],
    }


def write_export(entries: list[Entry], path: str) -> str:
    """Write the export to `path` with owner-only permissions.

    Created via os.open with 0600 rather than chmod-ed afterwards, so the
    plaintext is never momentarily readable by anyone else.
    """
    payload = build_payload(entries)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        raise ExportError(f"Could not write {path}: {exc}") from exc
    return path


def parse_export(text: str) -> list[Entry]:
    """Turn export JSON back into entries, or explain why it cannot.

    Entry ids are deliberately *not* reused.  In a .kdbx vault the id is the
    KeePass UUID, so importing with the original ids into a fresh vault would
    make the saver look for entries that do not exist there, and importing into
    a vault that still holds the originals would overwrite them instead of
    adding.  A new id on every imported entry makes both cases an ordinary add.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"This is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ImportError_("Expected a JSON object at the top level.")

    if payload.get("format") != FORMAT:
        raise ImportError_(
            "This file was not produced by amvault's export "
            f"(expected format {FORMAT!r})."
        )

    version = payload.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ImportError_(
            f"Export format version {version!r} is not supported by this "
            f"version of amvault (it understands {sorted(SUPPORTED_VERSIONS)})."
        )

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ImportError_("The file has no 'entries' list.")

    entries: list[Entry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ImportError_(f"Entry #{index + 1} is not a JSON object.")
        try:
            entry = Entry.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            title = raw.get("title", "<untitled>") if isinstance(raw, dict) else "?"
            raise ImportError_(
                f"Entry #{index + 1} ({title!r}) could not be read: {exc}"
            ) from exc
        entry.id = new_id()
        entries.append(entry)

    return entries


def read_export(path: str) -> list[Entry]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ImportError_(f"Could not read {path}: {exc}") from exc
    return parse_export(text)


def summarise(entries: list[Entry]) -> dict:
    """Counts worth showing before an import is committed."""
    attachments = sum(len(e.attachments) for e in entries)
    attachment_bytes = sum(a.size for e in entries for a in e.attachments)
    return {
        "entries": len(entries),
        "attachments": attachments,
        "attachment_bytes": attachment_bytes,
    }
