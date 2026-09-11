#!/usr/bin/env python3
"""amvault -- command line interface to the encrypted vault.

Scripting-friendly counterpart to the GUI.  Both share vault/, so a vault
written by one opens in the other.

Secrets are never printed unless explicitly asked for with --show, and the
master password is read from a TTY prompt rather than argv, since command
lines are visible to other processes and land in shell history.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from amvault_keys import (
    looks_like_private_key, looks_like_public_key, private_keys,
)
from vault.kdbx import KdbxCredentialsError, KdbxError, generate_key_file
from vault.ssh_agent import (
    SshAgentError, add_key, agent_available, list_agent_fingerprints, remove_key,
)
from vault.manager import (
    SyncConflict, VaultManager, estimate_strength, generate_passphrase, generate_password,
)
from vault.models import Category, Entry, TEMPLATE_FIELDS
from vault import portable
from vault import totp as totp_mod

def _default_vault_path() -> str:
    """Where the vault lives by default.

    Beside the application rather than under ~/.local/share, so copying this
    directory to a USB stick takes the vault, the backups and the app together
    and it all runs from wherever it is plugged in.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "data", "vault.kdbx")


DEFAULT_VAULT = _default_vault_path()


# --- output helpers -----------------------------------------------------
def err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def mask(value: str) -> str:
    return "•" * min(len(value), 12) if value else ""


def read_password(prompt: str = "Master password: ") -> str:
    """Read the master password without echoing it.

    Falls back to stdin when there is no TTY, so the CLI still works in a
    pipeline -- at the cost of the password possibly being visible to whatever
    produced it.
    """
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\n")


def open_vault(args) -> VaultManager:
    """Unlock the vault, turning crypto failures into clean CLI errors."""
    manager = VaultManager(args.vault)
    if not manager.exists():
        err(f"No vault at {args.vault}. Create one with:  amvault init")
        sys.exit(2)

    password = os.environ.get("AMVAULT_PASSWORD") or read_password()
    try:
        manager.unlock(password, args.key_file)
    except KdbxCredentialsError:
        err("Could not unlock: wrong password, wrong key file, or this is not "
            "an amvault/KeePass file.")
        if not args.key_file:
            err("If this vault uses a key file, pass --key-file PATH.")
        sys.exit(3)
    except KdbxError as exc:
        err(str(exc))
        sys.exit(3)
    return manager


def save_or_report(manager: VaultManager, force: bool = False) -> int:
    """Save, turning a sync conflict into an actionable message.

    Returns a process exit code: 0 on success, non-zero when the caller should
    stop.
    """
    try:
        manager.save(force=force)
        return 0
    except SyncConflict as conflict:
        err(str(conflict).replace("\n\n", " "))
        if conflict.backup_path:
            err(f"The version from the other device was copied to "
                f"{conflict.backup_path}")
        err("Re-run with --force to overwrite it with your changes.")
        return 5
    except (KdbxError, OSError) as exc:
        err(f"Could not save: {exc}")
        return 2


def print_entry(entry: Entry, show_secrets: bool = False) -> None:
    print(f"  id:       {entry.id}")
    print(f"  title:    {entry.title}")
    print(f"  category: {Category.LABELS.get(entry.category, entry.category)}")
    if entry.tags:
        print(f"  tags:     {', '.join(entry.tags)}")
    for name, value in entry.fields.items():
        if entry.is_secret_field(name) and not show_secrets:
            print(f"  {name+':':10}{mask(str(value))}  (use --show to reveal)")
        else:
            print(f"  {name+':':10}{value}")
    if entry.attachments:
        print("  attachments:")
        for att in entry.attachments:
            print(f"    - {att.name} ({att.size} bytes)")
    if entry.notes:
        print(f"  notes:    {entry.notes}")


# --- commands -----------------------------------------------------------
def cmd_init(args) -> int:
    manager = VaultManager(args.vault)
    if manager.exists():
        err(f"A vault already exists at {args.vault}.")
        return 2

    password = read_password("New master password: ")
    if not password:
        err("Master password cannot be empty.")
        return 2

    strength = estimate_strength(password)
    if strength["bits"] < 60:
        print(
            f"warning: that master password is {strength['label']} "
            f"(~{strength['bits']} bits). It protects everything else.",
            file=sys.stderr,
        )
    if read_password("Confirm master password: ") != password:
        err("Passwords do not match.")
        return 2

    key_file = args.key_file
    if args.generate_key_file:
        key_file = args.generate_key_file
        if os.path.exists(key_file):
            err(f"{key_file} already exists; refusing to overwrite it.")
            return 2
        generate_key_file(key_file)
        print(f"Key file written to {key_file}")
        print("Back this file up separately. Without it the vault cannot be opened.")

    try:
        manager.create(password, key_file)
    except (KdbxError, ValueError) as exc:
        err(str(exc))
        return 2

    print(f"Vault created at {args.vault}")
    return 0


def cmd_list(args) -> int:
    manager = open_vault(args)
    entries = manager.find(args.query or "", args.category)
    if not entries:
        print("No matching entries.")
        return 0

    width = max(len(e.title) for e in entries)
    for entry in entries:
        tags = f"  [{', '.join(entry.tags)}]" if entry.tags else ""
        marker = " 📎" if entry.attachments else ""
        print(f"{entry.id}  {entry.title:<{width}}  {entry.category}{tags}{marker}")
    print(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
    return 0


def _resolve(manager: VaultManager, needle: str) -> Entry | None:
    """Find an entry by exact id, else by unique title match."""
    entry = manager.get(needle)
    if entry:
        return entry
    matches = [e for e in manager.entries if e.title.lower() == needle.lower()]
    if not matches:
        matches = [e for e in manager.entries if needle.lower() in e.title.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        err(f"{needle!r} matches {len(matches)} entries; use an id instead:")
        for m in matches:
            print(f"  {m.id}  {m.title}", file=sys.stderr)
    return None


def cmd_show(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4
    print_entry(entry, args.show)
    return 0


def cmd_get(args) -> int:
    """Print one field only -- the form scripts actually want."""
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4
    if args.field not in entry.fields:
        err(f"Entry has no field {args.field!r}. Available: {', '.join(entry.fields) or '(none)'}")
        return 4
    print(entry.fields[args.field])
    return 0


def cmd_add(args) -> int:
    manager = open_vault(args)

    fields = {}
    for pair in args.field or []:
        if "=" not in pair:
            err(f"Field must be name=value, got {pair!r}")
            return 2
        name, value = pair.split("=", 1)
        fields[name.strip()] = value

    if args.prompt_secret:
        for name in args.prompt_secret:
            fields[name] = getpass.getpass(f"{name}: ")

    if args.generate:
        fields["password"] = generate_password(args.generate)
        print(f"Generated password: {fields['password']}")

    entry = Entry(
        title=args.title,
        category=args.category,
        fields=fields,
        tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
        notes=args.notes or "",
    )
    manager.add(entry)

    if args.attach:
        for path in args.attach:
            try:
                manager.attach_file(entry.id, path)
            except (OSError, ValueError) as exc:
                err(f"Could not attach {path}: {exc}")
                return 2

    rc = save_or_report(manager, args.force)
    if rc:
        return rc
    print(f"Added {entry.title} ({entry.id})")
    return 0


def cmd_set(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4

    if args.prompt:
        entry.fields[args.field] = getpass.getpass(f"{args.field}: ")
    else:
        entry.fields[args.field] = args.value or ""

    manager.update(entry)
    rc = save_or_report(manager, args.force)
    if rc:
        return rc
    print(f"Updated {args.field} on {entry.title}")
    return 0


def cmd_rm(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4

    if not args.yes:
        confirm = input(f"Delete {entry.title!r} and its {len(entry.attachments)} "
                        f"attachment(s)? [y/N] ")
        if confirm.lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0

    manager.delete(entry.id)
    rc = save_or_report(manager, args.force)
    if rc:
        return rc
    print(f"Deleted {entry.title}")
    return 0


def cmd_attach(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4
    try:
        att = manager.attach_file(entry.id, args.file, args.name)
    except (OSError, ValueError) as exc:
        err(str(exc))
        return 2
    rc = save_or_report(manager, args.force)
    if rc:
        return rc
    print(f"Attached {att.name} ({att.size} bytes) to {entry.title}")
    return 0


def cmd_export(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4

    if os.path.exists(args.dest) and not args.overwrite:
        err(f"{args.dest} exists; pass --overwrite to replace it.")
        return 2

    try:
        manager.export_attachment(entry.id, args.name, args.dest)
    except KeyError as exc:
        err(str(exc))
        return 4
    print(f"Wrote {args.dest} (mode 0600)")
    print("Remember to delete it when you are done: this copy is not encrypted.")
    return 0


def cmd_totp(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4

    secret = entry.fields.get("totp_secret")
    if not secret:
        err(f"{entry.title} has no totp_secret field.")
        return 4

    try:
        code = totp_mod.generate(secret)
    except totp_mod.TotpError as exc:
        err(str(exc))
        return 4

    remaining = totp_mod.seconds_remaining()
    if args.quiet:
        print(code)
    else:
        print(f"{code}   (valid {remaining}s)")
    return 0



def _pick_key_attachment(entry: Entry, wanted: str | None):
    """Choose which attachment on the entry is the private key.

    Public keys, certificates and notes sit alongside it, so the choice is made
    by inspecting each file's header.  The filename is only a tie-breaker.
    """
    if wanted:
        match = next((a for a in entry.attachments if a.name == wanted), None)
        if match is None:
            err(f"No attachment named {wanted!r} on {entry.title!r}.")
            err(f"Available: {', '.join(a.name for a in entry.attachments) or '(none)'}")
            return None
        if not looks_like_private_key(match.data):
            kind = "a public key" if looks_like_public_key(match.data) else "not a private key"
            err(f"{wanted!r} is {kind}; ssh-add cannot use it.")
            return None
        return match

    if not entry.attachments:
        err(f"{entry.title!r} has no attachments. Attach the private key file first.")
        return None

    # Content beats filename: this correctly ignores public keys and notes
    # whatever they are called.
    private = private_keys(entry)

    if not private:
        names = ", ".join(a.name for a in entry.attachments)
        err(f"{entry.title!r} has no private key among its attachments ({names}).")
        if any(looks_like_public_key(a.data) for a in entry.attachments):
            err("Only the public half (.pub) is stored — attach the private key too.")
        return None

    if len(private) > 1:
        err(f"{entry.title!r} holds several private keys; choose one with --name:")
        for a in private:
            err(f"  {a.name}")
        return None

    return private[0]


def cmd_ssh(args) -> int:
    """Load a key into ssh-agent, optionally run a command, then remove it.

    The host's own keys are untouched: ssh-agent holds many keys at once and
    tries them in turn, so this adds one for the duration and takes it back out.
    """
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4

    attachment = _pick_key_attachment(entry, args.name)
    if attachment is None:
        return 4

    before = list_agent_fingerprints()

    try:
        info = add_key(
            attachment.data,
            passphrase=entry.fields.get("passphrase") or None,
            lifetime_seconds=args.timeout,
            comment=attachment.name,
        )
    except SshAgentError as exc:
        err(str(exc))
        return 6

    if not info["ram_backed"]:
        err("warning: no RAM-backed filesystem was available, so the key was "
            "briefly written to ordinary disk.")

    added = list_agent_fingerprints() - before
    print(f"Loaded {attachment.name} from {entry.title!r} into ssh-agent.")
    if info["fingerprint"]:
        print(f"  {info['fingerprint']}")
    if args.timeout:
        print(f"  The agent will drop it automatically after {args.timeout}s.")
    if before:
        print(f"  {len(before)} existing agent key(s) left untouched.")

    # --keep hands control back with the key loaded; the user removes it later.
    if args.keep and not args.command:
        print("\nThe key stays loaded. Remove it with:")
        print(f"  amvault ssh {args.entry} --unload")
        return 0

    exit_code = 0
    try:
        if args.command:
            print(f"\n$ {' '.join(args.command)}")
            print("-" * 40)
            exit_code = subprocess.call(args.command)
            print("-" * 40)
        else:
            # No command: hold the key for an interactive shell so the user can
            # work, and unload when they leave it.
            shell = os.environ.get("SHELL", "/bin/sh")
            print(f"\nStarting a shell with the key loaded. Type 'exit' when done.")
            env = dict(os.environ)
            env["AMVAULT_SSH_ENTRY"] = entry.title
            env["PS1"] = f"(amvault:{entry.title}) $ "
            exit_code = subprocess.call([shell], env=env)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        # Runs on Ctrl+C and on a failing command alike, so the key does not
        # outlive the task it was loaded for.
        if added or not args.keep:
            if remove_key(attachment.data):
                print(f"Removed {attachment.name} from ssh-agent.")
            else:
                err("Could not remove the key from ssh-agent. Remove it with: "
                    "ssh-add -D  (this clears ALL keys)")

    return exit_code


def cmd_ssh_unload(args) -> int:
    manager = open_vault(args)
    entry = _resolve(manager, args.entry)
    if entry is None:
        err(f"No entry matching {args.entry!r}")
        return 4
    attachment = _pick_key_attachment(entry, args.name)
    if attachment is None:
        return 4
    if remove_key(attachment.data):
        print(f"Removed {attachment.name} from ssh-agent.")
        return 0
    err("That key was not in the agent (or could not be removed).")
    return 1


def cmd_ssh_list(args) -> int:
    if not agent_available():
        err("No ssh-agent is running. Start one with:  eval $(ssh-agent)")
        return 6
    fingerprints = list_agent_fingerprints()
    if not fingerprints:
        print("ssh-agent is running but holds no keys.")
        return 0
    print(f"ssh-agent holds {len(fingerprints)} key(s):")
    for fp in sorted(fingerprints):
        print(f"  {fp}")
    return 0


def cmd_gen(args) -> int:
    """Generate a password without touching the vault -- no unlock needed."""
    for _ in range(args.count):
        if args.words:
            value = generate_passphrase(args.words)
        else:
            value = generate_password(
                args.length,
                use_symbols=not args.no_symbols,
                avoid_ambiguous=args.readable,
            )
        strength = estimate_strength(value)
        if args.quiet:
            print(value)
        else:
            print(f"{value}   ({strength['label']}, ~{strength['bits']} bits)")
    return 0


def cmd_backup(args) -> int:
    manager = VaultManager(args.vault)
    try:
        dest = manager.backup(args.dest)
    except KdbxError as exc:
        err(str(exc))
        return 2
    print(f"Encrypted backup written to {dest}")
    return 0


def cmd_passwd(args) -> int:
    manager = open_vault(args)
    new_password = read_password("New master password: ")
    if not new_password:
        err("Master password cannot be empty.")
        return 2
    if read_password("Confirm new master password: ") != new_password:
        err("Passwords do not match.")
        return 2

    manager.change_password(new_password, args.new_key_file)
    print("Master password changed and the vault was re-encrypted with a fresh salt.")
    if args.new_key_file:
        print(f"The vault now also requires {args.new_key_file}")
    return 0


def cmd_stats(args) -> int:
    manager = open_vault(args)
    counts = manager.category_counts()
    total = sum(counts.values())
    print(f"Vault: {args.vault}")
    print(f"Entries: {total}")
    for category, count in counts.items():
        if count:
            print(f"  {Category.LABELS[category]:<24} {count}")
    tags = manager.all_tags()
    if tags:
        print(f"Tags: {', '.join(tags)}")
    attachments = sum(len(e.attachments) for e in manager.entries)
    if attachments:
        print(f"Attachments: {attachments}")
    return 0


def cmd_dump(args) -> int:
    """Export everything as plaintext JSON, attachments included.

    The intended use is key rotation: dump the old vault, `init` a new one with
    a fresh password and key file, then `load` the dump into it.  Attachments
    ride along base64-encoded inside the same file, so nothing has to be
    exported and re-attached by hand.

    Deliberately hard to do by accident -- everything the vault protects is
    readable in the result.
    """
    manager = open_vault(args)
    if not args.i_understand_this_is_plaintext:
        err("Refusing to write plaintext secrets without "
            "--i-understand-this-is-plaintext.")
        return 2

    entries = manager.entries
    if args.output:
        try:
            portable.write_export(entries, args.output)
        except portable.ExportError as exc:
            err(str(exc))
            return 2
        counts = portable.summarise(entries)
        print(f"Wrote {counts['entries']} entries and {counts['attachments']} "
              f"attachments to {args.output} (mode 0600).", file=sys.stderr)
        print("This file is PLAINTEXT. Delete it as soon as you have "
              "imported it.", file=sys.stderr)
    else:
        json.dump(portable.build_payload(entries), sys.stdout,
                  indent=2, ensure_ascii=False)
        print()
    return 0


def cmd_load(args) -> int:
    """Import entries from a plaintext JSON export into this vault.

    Entries are always *added*, never merged onto existing ones: imported
    entries get fresh ids, so importing into a vault that already holds the
    same data gives you two copies rather than silently overwriting one.  For
    the rotation case the target is a brand-new vault, where that is exactly
    right.
    """
    try:
        entries = portable.read_export(args.file)
    except portable.ImportError_ as exc:
        err(str(exc))
        return 2

    counts = portable.summarise(entries)
    manager = open_vault(args)
    existing = len(manager.entries)

    if existing and not args.merge:
        err(f"This vault already has {existing} entries. Importing would add "
            f"{counts['entries']} more alongside them, not replace them.")
        err("Pass --merge if that is what you want, or import into a fresh "
            "vault made with `amvault init`.")
        return 2

    for entry in entries:
        manager.add(entry)

    code = save_or_report(manager, force=args.force)
    if code:
        return code

    print(f"Imported {counts['entries']} entries and {counts['attachments']} "
          f"attachments ({counts['attachment_bytes'] / 1024:.0f} KiB) "
          f"into {args.vault}.")
    if existing:
        print(f"The vault now holds {existing + counts['entries']} entries.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amvault",
        description="Encrypted KeePass vault for passwords, SSH keys, API tokens "
                "and network gear. Readable by KeePassDX / Strongbox on mobile.",
    )
    parser.add_argument("--vault", default=os.environ.get("AMVAULT_PATH", DEFAULT_VAULT),
                        help=f"path to the vault file (default: {DEFAULT_VAULT})")
    parser.add_argument("--key-file", help="key file required to unlock (second factor)")
    parser.add_argument("--force", action="store_true",
                        help="save even if the vault changed on another device")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new vault")
    p.add_argument("--generate-key-file", metavar="PATH",
                   help="generate a random key file and require it to unlock")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("list", help="list entries")
    p.add_argument("query", nargs="?", help="filter by text")
    p.add_argument("-c", "--category", choices=Category.ALL)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one entry")
    p.add_argument("entry", help="entry id or title")
    p.add_argument("-s", "--show", action="store_true", help="reveal secret fields")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("get", help="print a single field (for scripts)")
    p.add_argument("entry")
    p.add_argument("field")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("add", help="add an entry")
    p.add_argument("title")
    p.add_argument("-c", "--category", choices=Category.ALL, default=Category.LOGIN)
    p.add_argument("-f", "--field", action="append", metavar="NAME=VALUE")
    p.add_argument("-p", "--prompt-secret", action="append", metavar="NAME",
                   help="prompt for this field without echoing it")
    p.add_argument("-g", "--generate", type=int, nargs="?", const=24, metavar="LEN",
                   help="generate a random password of LEN characters")
    p.add_argument("-t", "--tags", help="comma-separated tags")
    p.add_argument("-n", "--notes")
    p.add_argument("-a", "--attach", action="append", metavar="FILE")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("set", help="set a field on an existing entry")
    p.add_argument("entry")
    p.add_argument("field")
    p.add_argument("value", nargs="?")
    p.add_argument("-p", "--prompt", action="store_true", help="prompt for the value")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("rm", help="delete an entry")
    p.add_argument("entry")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("attach", help="attach a file to an entry")
    p.add_argument("entry")
    p.add_argument("file")
    p.add_argument("--name", help="store under a different name")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("export", help="write an attachment back to disk")
    p.add_argument("entry")
    p.add_argument("name", help="attachment name")
    p.add_argument("dest", help="destination path")
    p.add_argument("--overwrite", action="store_true",
                   help="overwrite the destination file if it exists")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("totp", help="print the current 2FA code for an entry")
    p.add_argument("entry")
    p.add_argument("-q", "--quiet", action="store_true", help="print only the code")
    p.set_defaults(func=cmd_totp)


    p = sub.add_parser("ssh", help="load an SSH key into ssh-agent temporarily")
    p.add_argument("entry", help="entry id or title holding the key")
    p.add_argument("command", nargs="*",
                   help="command to run with the key loaded (default: a shell)")
    p.add_argument("--name", help="which attachment is the private key")
    p.add_argument("-t", "--timeout", type=int, default=900, metavar="SEC",
                   help="agent drops the key after this long (default 900, 0 = never)")
    p.add_argument("--keep", action="store_true",
                   help="leave the key loaded instead of removing it afterwards")
    p.add_argument("--unload", action="store_true",
                   help="just remove this entry's key from the agent")
    p.set_defaults(func=lambda a: cmd_ssh_unload(a) if a.unload else cmd_ssh(a))

    p = sub.add_parser("ssh-list", help="show keys currently in ssh-agent")
    p.set_defaults(func=cmd_ssh_list)

    p = sub.add_parser("gen", help="generate a password (no vault needed)")
    p.add_argument("-l", "--length", type=int, default=24)
    p.add_argument("-w", "--words", type=int, metavar="N",
                   help="generate an N-word passphrase instead")
    p.add_argument("-n", "--count", type=int, default=1)
    p.add_argument("--no-symbols", action="store_true")
    p.add_argument("--readable", action="store_true",
                   help="avoid characters that look alike (Il1O0)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("backup", help="copy the encrypted vault to a timestamped file")
    p.add_argument("dest", nargs="?", help="destination directory")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("passwd", help="change the master password")
    p.add_argument("--new-key-file", help="also require this key file from now on")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("stats", help="summarise the vault")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("dump", help="export all entries (and attachments) as "
                                    "plaintext JSON")
    p.add_argument("-o", "--output", help="write to a file instead of stdout")
    p.add_argument("--i-understand-this-is-plaintext", action="store_true")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("load", help="import entries from a plaintext JSON dump")
    p.add_argument("file", help="the JSON file written by `amvault dump`")
    p.add_argument("--merge", action="store_true",
                   help="allow importing into a vault that already has entries")
    p.set_defaults(func=cmd_load)

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Everything after a bare "--" is a command to run, not our own options.
    # argparse would swallow the separator, so take it out first.
    trailing: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, trailing = argv[:index], argv[index + 1:]

    args = build_parser().parse_args(argv)
    if trailing:
        if not hasattr(args, "command"):
            err("This command does not take a trailing '-- command'.")
            return 2
        args.command = list(getattr(args, "command", [])) + trailing
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # e.g. `amvault list | head` -- not an error worth reporting.
        return 0


if __name__ == "__main__":
    sys.exit(main())
