#!/usr/bin/env bash
# Vault housekeeping: reset, backup, restore, and status.
#
# Every destructive action archives what it is about to remove first, so a
# mistaken reset is recoverable as long as the master password is known.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
# Beside the app, so the whole directory is portable on a USB stick.
VAULT="${AMVAULT_PATH:-$HERE/data/vault.kdbx}"
BACKUP_DIR="${AMVAULT_BACKUPS:-$HERE/data/backups}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

usage() {
    cat <<USAGE
Usage: $(basename "$0") <command>

  status              Show which vault files exist and how big they are
  backup              Copy the vault to $BACKUP_DIR with a timestamp
  restore <file>      Put a backup back in place (archives the current one)
  reset               Delete the vault and start over (archives it first)
  list-backups        Show available backups

The vault is a single encrypted file: $VAULT
A key file, if you use one, is separate and is NOT backed up by this script --
back it up yourself, somewhere other than the vault.
USAGE
}

cmd_status() {
    echo "Vault path: $VAULT"
    if [[ -f "$VAULT" ]]; then
        grn "  exists  ($(stat -c%s "$VAULT") bytes, modified $(stat -c%y "$VAULT" | cut -d. -f1))"
    else
        ylw "  does not exist yet — the app will offer to create it"
    fi

    # Legacy files from the pre-KeePass format, if any are still around.
    local legacy="${VAULT%.kdbx}.amv"
    if [[ -f "$legacy" ]]; then
        ylw "Legacy vault still present: $legacy"
        echo "  Convert it with:  $PY $HERE/migrate_to_kdbx.py '$legacy' '$VAULT'"
    fi

    # Conflict copies mean two devices diverged and were never reconciled.
    local conflicts
    conflicts=$(find "$(dirname "$VAULT")" -maxdepth 1 \
        \( -name '*.conflict-*.kdbx' -o -name '*.mine-*.kdbx' \) 2>/dev/null | wc -l)
    if [[ "$conflicts" -gt 0 ]]; then
        ylw "$conflicts unresolved sync-conflict file(s) in $(dirname "$VAULT")"
    fi

    if [[ -d "$BACKUP_DIR" ]]; then
        echo "Backups: $(find "$BACKUP_DIR" -name '*.kdbx' 2>/dev/null | wc -l) in $BACKUP_DIR"
    else
        ylw "No backups yet. Run: $(basename "$0") backup"
    fi
}

cmd_backup() {
    [[ -f "$VAULT" ]] || { red "No vault at $VAULT"; exit 1; }
    mkdir -p "$BACKUP_DIR"
    local dest="$BACKUP_DIR/vault-$(date +%Y%m%d-%H%M%S).kdbx"
    cp -p "$VAULT" "$dest"
    chmod 600 "$dest" 2>/dev/null || true
    grn "Backed up to $dest"
    echo "Still encrypted — safe to copy to a USB stick or cloud storage."
    echo "It opens with the master password the vault had at this moment."
}

cmd_list_backups() {
    [[ -d "$BACKUP_DIR" ]] || { ylw "No backups yet."; exit 0; }
    find "$BACKUP_DIR" -name '*.kdbx' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2- | while read -r f; do
        printf '%s  (%s bytes, %s)\n' "$f" "$(stat -c%s "$f")" \
            "$(stat -c%y "$f" | cut -d. -f1)"
    done
}

cmd_restore() {
    local src="${1:-}"
    [[ -n "$src" ]] || { red "Which backup? Run: $(basename "$0") list-backups"; exit 2; }
    [[ -f "$src" ]] || { red "No such file: $src"; exit 1; }

    # Refuse to restore something that is not actually a vault, before it
    # displaces a working one.
    if ! head -c4 "$src" | grep -q $'\x03\xd9\xa2\x9a'; then
        red "$src does not look like a KeePass file. Refusing."
        exit 1
    fi

    if [[ -f "$VAULT" ]]; then
        mkdir -p "$BACKUP_DIR"
        local archived="$BACKUP_DIR/vault-replaced-$(date +%Y%m%d-%H%M%S).kdbx"
        cp -p "$VAULT" "$archived"
        ylw "Current vault archived to $archived"
    fi

    mkdir -p "$(dirname "$VAULT")"
    cp -p "$src" "$VAULT"
    chmod 600 "$VAULT" 2>/dev/null || true
    grn "Restored $src -> $VAULT"
    echo "Unlock it with the master password that vault had when it was backed up."
}

cmd_reset() {
    echo "This deletes the vault and everything in it:"
    echo "  $VAULT"
    echo
    if [[ ! -f "$VAULT" ]]; then
        ylw "Nothing to delete — no vault at that path."
    else
        echo "A copy will be archived first, so this is reversible if you still"
        echo "know the master password."
        echo
    fi
    read -r -p "Type RESET to confirm: " answer
    [[ "$answer" == "RESET" ]] || { echo "Cancelled."; exit 0; }

    if [[ -f "$VAULT" ]]; then
        mkdir -p "$BACKUP_DIR"
        local archived="$BACKUP_DIR/vault-before-reset-$(date +%Y%m%d-%H%M%S).kdbx"
        cp -p "$VAULT" "$archived"
        rm -f "$VAULT"
        grn "Deleted $VAULT"
        echo "Archived copy: $archived"
    fi

    # Sweep the debris a reset is meant to clear, but leave key files alone --
    # deleting one would destroy access to every backup that used it.
    find "$(dirname "$VAULT")" -maxdepth 1 \
        \( -name '*.conflict-*.kdbx' -o -name '*.mine-*.kdbx' \) -delete 2>/dev/null || true

    echo
    grn "Start fresh with:  $PY $HERE/amvault-gui.py"
}

case "${1:-}" in
    status)        cmd_status ;;
    backup)        cmd_backup ;;
    restore)       shift; cmd_restore "${1:-}" ;;
    reset)         cmd_reset ;;
    list-backups)  cmd_list_backups ;;
    ""|-h|--help)  usage ;;
    *)             red "Unknown command: $1"; echo; usage; exit 2 ;;
esac
