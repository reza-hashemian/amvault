# Net Tool

A desktop companion to [amvault](../%5B03%5D-account-manager) for working with
network devices: list them, open an SSH session in your own terminal, and look
up the command you need without leaving the window.

It is a separate program, but it keeps no passwords of its own. amvault starts
it and answers its requests from the unlocked vault, so it opens without asking
for the master password again and there is nothing to keep in sync.

## What it does today

- **Devices** — every Network Device entry in the vault, plus SSH key entries
  that name a host, grouped by platform (Cisco IOS, MikroTik, Linux, Windows).
  A dot beside each one shows whether it answers ping, re-checked every 20
  seconds, with the round-trip time.
- **Scan network** — pings every address in a range (up to 1024 at a time),
  reads the neighbour table for MAC addresses and hosts that ignore ping, and
  checks a handful of management ports to hint at what each host is. Anything
  not in the vault yet can be added with one click; you then fill in its
  sign-in details in amvault. Needs no administrator rights.
- **Dashboard** — tick the devices to watch. Each is pinged every 5 seconds;
  Linux, MikroTik, Cisco IOS and Windows machines are also read over SSH for CPU, memory,
  uptime and per-port traffic. Live charts cover the last hour, and a report
  (availability, averages, peaks) covers up to 30 days and exports to CSV.
  Sign-in details are read from the vault once, at start, and kept in memory
  only — so monitoring continues if the vault locks, and ends when you stop it
  or close the window.
- **Remote desktop** — opens the right program already pointed at the machine.
  Windows machines reachable by address open in RDP (FreeRDP on Linux, signed
  in from the vault; the password goes to the program's stdin, never its
  arguments). Machines with no fixed address open in RustDesk by the ID stored
  in the entry's `rustdesk_id` field, which works from behind NAT the way
  AnyDesk does. The tool starts these programs; it does not carry screens
  itself.
- **Manage** — for MikroTik, Cisco IOS, Linux and Windows, a panel that signs in and
  shows the device as tables: interfaces or ports, VLANs, trunks, addresses,
  routes, DHCP leases, firewall rules, the MAC table; on Linux services,
  listening ports, disks and processes. Common changes are offered: on Cisco
  port description, access VLAN, make trunk, add or remove trunk VLANs, shut
  down / bring up, new VLAN, save configuration; on MikroTik enable / disable
  and comment; on Linux and Windows start, stop and restart a service (on
  Linux through `sudo -n` unless signed in as root). Windows is reached through
  its OpenSSH server and asked in PowerShell. Every change
  first shows the exact commands and is sent only when you confirm. On Cisco,
  the vault's `enable_password` is used to enter privileged mode; without it
  the panel is view-only.
- **SSH** — double-click a device, or press *Open SSH in Terminal*. The session
  opens in your system terminal with your own shell, colours and settings.
- **Command guide** — per-platform reference with the general form of each
  command, what every part means, and a paste-ready example. Shown beside a
  device for its own platform, or browsed from the sidebar.

Planned next: more changes in the panels (addresses, firewall rules).

## How it fits together

```
amvault (Python/Qt)            Net Tool
┌──────────────────┐   stdin   ┌─────────────────────────────────────┐
│ unlocked vault   │──token──▶ │ shell (Rust/Tauri) ── window (Svelte)│
│ credential broker│◀─socket── │   └─ engine (Python) : network logic │
└──────────────────┘           └─────────────────────────────────────┘
```

- **Shell** (`src-tauri/`) owns the window and relays requests. Nothing else.
- **Window** (`src/`) is the interface.
- **Engine** (`nettool_engine/`) is where network work happens, in Python
  because that is where the device libraries are.
- **Guides** (`guides/*.json`) are plain data; edit them to add commands.

### What crosses the line to amvault

| The tool can | The tool cannot |
|---|---|
| List device entries, without their secrets | See logins, API keys, notes or any non-device entry |
| Ask amvault to open an SSH session | Read custom fields outside a fixed allowlist |
| Fetch one device's credentials when it connects to it | Do anything while the vault is locked |
| Add a new device entry: a name and an address | Add a secret, or change or delete an existing entry |

The token that authenticates the tool is random per launch, travels on stdin
(never argv or the environment), and the endpoint it unlocks accepts a single
connection and is then removed.

## Install

Installers are on the releases page: a `.deb` for Debian and Ubuntu, a setup
`.exe` for Windows and a `.dmg` for macOS (Apple silicon). Each carries the app
and its engine; nothing else needs installing. amvault finds an installed copy
on its own.

```bash
sudo apt install ./nettool_0.1.0_amd64.deb
```

The Windows and macOS installers are not code-signed, so the system warns the
first time: on Windows choose *More info → Run anyway*; on macOS right-click
the app and choose *Open*.

A release is made by pushing a version tag; `.github/workflows/release.yml`
builds all three and attaches them:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

## Build

Needs Rust, Node 20+, and on Linux the WebKitGTK development package
(`libwebkit2gtk-4.1-dev`).

```bash
npm install
npm run tauri build -- --no-bundle     # → src-tauri/target/release/nettool

python3 -m venv .venv                  # the engine's own libraries (SSH)
./.venv/bin/pip install -r requirements.txt
```

Without the `.venv` everything still works except reading CPU, memory and
traffic; the dashboard says so and falls back to ping.

amvault looks for that binary in a folder named `*net-tool*` beside its own, or
at the path in `AMVAULT_NETTOOL`. Then, in amvault: **Network tool** (Ctrl+T).

## What it stores

In `~/.local/share/nettool` (`%LOCALAPPDATA%\nettool` on Windows):

- `history.db` — measurements for reports: device names, addresses, numbers.
- `known_hosts` — the SSH host key of each device, pinned the first time it is
  monitored. A device whose key later changes is refused until its line is
  removed, so a swapped or impersonated device cannot collect the password.

Never a password or key.

## Develop

```bash
npm run dev                            # interface only, in a browser, sample data
NETTOOL_DEMO=1 npm run tauri dev       # the real window, sample data
./.venv/bin/python -m unittest discover -s tests  # engine tests
```
