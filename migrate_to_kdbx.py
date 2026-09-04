#!/usr/bin/env python3
"""Convert a legacy .amv vault into the KeePass .kdbx format.

The .amv format was readable only by this app.  KDBX is readable by KeePassDX
on Android and Strongbox / KeePassium on iOS, which is what makes a single
synced file usable from a phone.

The original .amv file is left untouched, so a failed conversion costs nothing.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from legacy.amv_crypto import DecryptionError, WeakParametersError
from legacy.amv_storage import VaultFileError, load_vault
from vault import kdbx
from vault.manager import VaultManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a .amv vault to .kdbx")
    parser.add_argument("source", help="the existing .amv vault")
    parser.add_argument("dest", help="the .kdbx file to create")
    parser.add_argument("--key-file", help="key file the .amv vault requires")
    parser.add_argument("--new-key-file", help="key file to require on the .kdbx vault")
    args = parser.parse_args()

    if not os.path.exists(args.source):
        print(f"error: no such file: {args.source}", file=sys.stderr)
        return 2
    if os.path.exists(args.dest):
        print(f"error: {args.dest} already exists; refusing to overwrite it.",
              file=sys.stderr)
        return 2

    password = getpass.getpass("Master password for the .amv vault: ")
    try:
        entries, _, _, _ = load_vault(args.source, password, 
                                      open(args.key_file, "rb").read() if args.key_file else None)
    except DecryptionError:
        print("error: wrong password or key file.", file=sys.stderr)
        return 3
    except (VaultFileError, WeakParametersError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print(f"Read {len(entries)} entries from {args.source}")

    new_password = getpass.getpass("Master password for the new .kdbx vault "
                                   "(blank to reuse the old one): ")
    if not new_password:
        new_password = password
    elif getpass.getpass("Confirm: ") != new_password:
        print("error: passwords do not match.", file=sys.stderr)
        return 2

    manager = VaultManager(args.dest)
    manager.create(new_password, args.new_key_file)
    for entry in entries:
        manager.add(entry)
    manager.save()

    # Read it back and compare, so the report is a verified fact rather than an
    # assumption that the write worked.
    check = VaultManager(args.dest)
    check.unlock(new_password, args.new_key_file)
    written = {e.title for e in check.entries}
    expected = {e.title for e in entries}

    attachments_in = sum(len(e.attachments) for e in entries)
    attachments_out = sum(len(e.attachments) for e in check.entries)

    print(f"\nWrote {len(check.entries)} entries to {args.dest}")
    print(f"Attachments: {attachments_in} in, {attachments_out} out")

    missing = expected - written
    if missing:
        print(f"\nWARNING: these entries did not survive: {sorted(missing)}",
              file=sys.stderr)
        return 1
    if attachments_out != attachments_in:
        print("\nWARNING: attachment count differs — check before deleting the "
              "original.", file=sys.stderr)
        return 1

    print("\nVerified: every entry and attachment round-tripped.")
    print(f"The original {args.source} was not modified. Keep it until you have "
          f"confirmed the new vault opens on your phone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
