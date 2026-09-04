# amvault

Encrypted vault for passwords, SSH keys, API tokens, and network device
credentials. Desktop GUI plus a scripting CLI — storing everything in the
**KeePass `.kdbx` format**, so the same file opens on your phone.

## One file, both devices

```
Phone (KeePassDX / Strongbox)  ←→  vault.kdbx  ←→  Desktop (amvault)
```

Because the storage format is standard KeePass, no bridge or server is needed.
Sync the file with Syncthing, a USB cable, or any cloud storage — it is
encrypted at rest, so an untrusted cloud is fine.

| Platform | App |
|---|---|
| Android | **KeePassDX** or Keepass2Android (both open source) |
| iOS | **Strongbox** or KeePassium |
| Desktop | this app, or KeePassXC |

### What the phone sees

| amvault | KeePass field | Result on mobile |
|---|---|---|
| Category | Group (folder) | Entries organised into folders |
| username / password / url | The standard fields | Autofill and "copy password" work |
| Other fields (`mgmt_ip`, `vendor`, …) | Custom string fields | Visible as extra fields |
| Secret fields (`enable_password`, `passphrase`) | **Protected** custom fields | Masked, same as passwords |
| `totp_secret` | `otp` (otpauth:// URI) | Phone generates the same 2FA codes |
| Attachments (SSH keys) | Binaries | Downloadable from the entry |

Fields another client added are preserved on save — editing on the desktop does
not drop what you created on the phone.

## Security design

| Layer | Choice | Why |
|---|---|---|
| Format | KDBX 4 | Argon2id KDF + AES-256, and portable to every platform |
| Second factor | Key file | Its bytes are part of the key, so the vault cannot be opened without it |
| Writes | temp file → rename | A crash mid-save leaves the old vault or the new one, never a truncated file |
| Sync safety | mtime+size check before every save | Detects an edit synced from the phone instead of overwriting it |

### Why a key file and not a TOTP prompt

Some managers offer a TOTP code as a "second factor". For an offline vault that
adds no cryptographic strength: the code is checked by the software, so anyone
holding the file can patch the check out. A key file goes into the key
derivation itself — without it the key cannot be derived at all.

**The tradeoff:** lose the key file and the vault is gone permanently. Back it
up separately from the vault.

**On mobile:** if you copy the key file to the phone, both factors live on one
device and the second factor stops being meaningful there. A long master
password plus the app's biometric lock is usually the better phone-side choice.

## Portable by design

The vault lives beside the app, not in your home directory:

```
[03]-account-manager/
├── data/
│   ├── vault.kdbx      ← your vault
│   └── backups/        ← timestamped copies
├── amvault-gui.py
└── vault-admin.sh
```

Copy this whole directory to a USB stick and it runs from there — app, vault
and backups together. `data/` is gitignored, so secrets never reach a repo.

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install "cryptography>=42" argon2-cffi PySide6 pyperclip pykeepass
```

## Migrating from the old .amv format

Earlier versions used a private format that phones cannot read:

```bash
./.venv/bin/python migrate_to_kdbx.py old.amv new.kdbx
```

It verifies every entry and attachment round-tripped, and never modifies the
original. Keep the `.amv` until you have confirmed the new file opens on your
phone.

## GUI

```bash
./.venv/bin/python amvault-gui.py               # ~/.local/share/amvault/vault.kdbx
./.venv/bin/python amvault-gui.py /path/to.kdbx
```

Auto-locks after 10 minutes idle. Clipboard copies clear after 30 seconds, and
only if the clipboard still holds what was copied. TOTP codes refresh live with
a countdown.

## CLI

```bash
alias amvault='"$PWD/.venv/bin/python" "$PWD/amvault.py"'

amvault init                                  # create a vault
amvault init --generate-key-file ~/vault.key  # ... with a key file

amvault add "GitHub" -f username=me -g 32 -t work,dev
amvault add "Core Switch" -c network -f mgmt_ip=10.0.0.1 -p password
amvault add "Prod" -c ssh_key -f host=10.0.0.5 -a ~/.ssh/id_ed25519

amvault list                     # all entries
amvault show "Core Switch"       # secrets masked
amvault show "Core Switch" -s    # revealed
amvault get GitHub username      # one field, for scripts
amvault totp Gitlab              # current 2FA code

amvault export "Prod" id_ed25519 /tmp/key   # writes mode 0600
amvault gen -l 32 -n 5           # generate passwords (no unlock needed)
amvault backup ~/backups
amvault passwd                   # change master password
```

Passwords are read from a TTY prompt, never argv. `AMVAULT_PASSWORD` is honoured
for automation.

## Connecting to a server

An entry that carries a `host` (or `mgmt_ip`) gets a **Connect** bar in the GUI:
one click loads the entry's key into ssh-agent and opens a terminal running
`ssh`. No copy-pasting the address, port, user or key.

**How it knows which key belongs to which server:** it doesn't have to work it
out — the key is an attachment *on the same entry* as the host. One entry means
one server, its address, its user, and its key together. If an entry holds two
private keys the app asks which to use rather than guessing.

ssh-agent itself has no host-to-key mapping; it offers every key it holds, in
turn. With several loaded, a server can hit its `MaxAuthTries` limit before
reaching the right one, so the connection is scoped with `IdentitiesOnly` to
the single key that entry carries.

## Using SSH keys

`amvault ssh` loads a key into ssh-agent for the length of a task and takes it
back out afterwards. The key is staged in `/dev/shm` (RAM), handed to `ssh-add`,
and unlinked immediately — it never reaches persistent storage.

```bash
# Run one command with the key loaded, then unload it
amvault ssh GitHub -- git push origin main

# Log into a server
amvault ssh "Prod Server" -- ssh root@10.0.0.5

# Open a shell with the key available; it unloads when you exit
amvault ssh GitHub

# Leave it loaded to use across several commands
amvault ssh GitHub --keep
amvault ssh GitHub --unload      # ... and remove it when done

amvault ssh-list                 # what is in the agent right now
```

**Your host key is never touched.** ssh-agent holds several keys at once and
offers them in turn, so a vault key is added alongside whatever you normally
use, and removing it leaves the agent exactly as it was. The removal runs even
if the command fails or you press Ctrl+C.

As a safety net the agent is also told to drop the key by itself after 15
minutes (`-t 0` disables this, `-t SEC` changes it), so a session that dies
without cleanup does not leave the key loaded forever.

If the key is encrypted, put its passphrase in the entry's `passphrase` field
and it is supplied automatically.

## Sync conflicts

If the vault changed on disk since it was opened — the usual result of editing
on both devices between syncs — saving stops and asks:

- **Keep my changes** — overwrite the synced version (a copy of it is saved as
  `vault.kdbx.conflict-<timestamp>.kdbx` first, so nothing is lost).
- **Load from disk** — discard local edits and adopt the phone's version.

On the CLI the same situation exits non-zero; re-run with `--force` to keep your
version.

There is **no automatic merge**. Two vaults edited independently cannot be
reconciled without guessing which side is right, so the choice stays with you.
The practical habit: let the sync settle before editing on the other device.

## Layout

```
vault/       kdbx.py  models.py  manager.py  totp.py  ssh_agent.py
gui/         unlock.py  main_window.py  editor.py  password_dialog.py  theme.py
legacy/      readers for the old .amv format, used only by the migration tool
amvault.py       CLI
amvault-gui.py   GUI entry point
tests/           55 tests
vault-admin.sh   status / backup / restore / reset
```

```bash
./.venv/bin/python -m unittest discover -s tests
```

Tests cover RFC 6238 TOTP vectors, the full KeePass field mapping, sync-conflict
detection and resolution, entries written by other clients, attachment
round-trips, and that loading an SSH key leaves the host's own agent keys
untouched.

## Housekeeping

```bash
./vault-admin.sh status          # what exists, and where
./vault-admin.sh backup          # timestamped encrypted copy
./vault-admin.sh list-backups
./vault-admin.sh restore <file>  # archives the current vault first
./vault-admin.sh reset           # start over (archives first; asks for RESET)
```

Backups are still encrypted, so they are safe on a USB stick or in cloud
storage. Each opens with the master password the vault had when it was taken.

A **key file is never backed up by these commands** — back it up yourself, and
somewhere other than the vault, or it defeats the purpose.

## Platform support

| | Linux | macOS | Windows |
|---|---|---|---|
| Vault, GUI, CLI, TOTP | ✅ | ✅ | ✅ |
| SSH connect / agent loading | ✅ | ✅ | ✅ |
| Key staged in RAM (`/dev/shm`) | ✅ | ✅ | ❌ private temp dir instead |
| Passphrase supplied automatically | ✅ | ✅ | ❌ Windows OpenSSH ignores `SSH_ASKPASS` |

Windows needs OpenSSH installed (Settings → Optional Features). The app uses
Windows Terminal when present and falls back to a console window.

On Windows there is no RAM-backed filesystem available to user code, so the key
is written to the per-user temp directory with an owner-only ACL and deleted
immediately. That is a weaker guarantee than on Linux, and the app reports which
one applied.

## Honest limitations

- **Not audited.** The KDBX format and `pykeepass` are widely used, but this
  app's code around them has had no outside review. For your highest-value
  secrets, KeePassXC is the better-tested client for the very same file — and
  since the format is shared, you can use both.
- **Keys are not wiped from RAM.** Python cannot reliably zero immutable
  objects, so locking drops references but cannot guarantee overwriting.
- **No swap protection.** Decrypted data can reach swap. Use encrypted swap.
- **TOTP seeds next to passwords** means one compromised vault yields both
  factors. Keep high-value seeds on a separate device.
- **Whole-vault rewrite on save**, so large attachments slow saves and bloat the
  file you sync. Attachments are capped at 5 MiB.
- **Back up the vault.** Encryption protects against theft, not disk failure.
- **A loaded SSH key is available to your whole session.** While it is in
  ssh-agent, any process running as you can ask the agent to sign with it. That
  is true of any agent-based workflow; `--keep` widens the window, so prefer the
  one-command form.
