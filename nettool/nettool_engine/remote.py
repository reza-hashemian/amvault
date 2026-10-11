"""Open a remote desktop to a machine, in the program built for it.

The tool does not carry screens itself.  Remote desktop done well -- fast
screen encoding, input, getting through NAT, and doing all of it securely -- is
a product in its own right, and good open ones exist.  So this picks the right
one for the machine and starts it already pointed at the target:

  * RDP, for a Windows machine reachable by address (the same LAN, or a VPN).
    The user and password come from the vault, so it opens signed in.
  * RustDesk, for a machine with no fixed address.  Like AnyDesk it is reached
    by an ID through a rendezvous server, so it works from behind NAT.  The
    vault entry holds the ID in a `rustdesk_id` field.

RustDesk asks for the machine's password itself, once, and can remember it;
it is not passed on the command line, where other users of this computer
could read it from the process list.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

# FreeRDP's names across versions and display servers, newest first.
_FREERDP = ("xfreerdp3", "sdl-freerdp3", "wlfreerdp3", "xfreerdp", "wlfreerdp")
_RUSTDESK = ("rustdesk",)


class RemoteError(Exception):
    pass


def _find(names) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def available(device: dict) -> list[str]:
    """Which kinds of remote desktop this device's entry supports."""
    kinds = []
    if device.get("platform") == "windows" and device.get("address"):
        kinds.append("rdp")
    if rustdesk_id(device):
        kinds.append("rustdesk")
    return kinds


def rustdesk_id(device: dict) -> str:
    """The entry's RustDesk ID with its spacing removed, or "" if it has none
    or it is not an ID (it becomes a command-line argument)."""
    raw = str((device.get("fields") or {}).get("rustdesk_id") or "").strip()
    if raw.startswith("-"):
        return ""
    raw = re.sub(r"[\s-]", "", raw)
    return raw if re.fullmatch(r"[A-Za-z0-9_.@:]{4,64}", raw) else ""


def rdp_command(client: str, address: str, username: str) -> list[str]:
    """FreeRDP's arguments.  The password is not among them: it goes to the
    program's standard input, which no other process can read."""
    name = client.replace("\\", "/").rsplit("/", 1)[-1]
    # Trust the server's certificate on first use and pin it, as with SSH.
    # The option was renamed between FreeRDP 2 and 3.
    trust = "/cert:tofu" if name.endswith("3") else "/cert-tofu"
    command = [client, f"/v:{address}", trust, "/dynamic-resolution", "+clipboard", "/from-stdin"]
    if username:
        domain, slash, user = username.rpartition("\\")
        command.append(f"/u:{user}")
        if slash and domain:
            command.append(f"/d:{domain}")
    return command


def open_rdp(device: dict, credentials: dict, popen=subprocess.Popen) -> dict:
    address = device.get("address") or ""
    if not address or address.startswith("-") or not re.fullmatch(r"[A-Za-z0-9.:\[\]_-]+", address):
        raise RemoteError("This entry has no usable address.")
    username = str(credentials.get("username") or "")
    password = str(credentials.get("password") or "")

    if IS_WINDOWS:
        # Windows' own client. It has no safe way to be handed a password, so
        # it asks; the address is filled in.
        popen(["mstsc", f"/v:{address}"])
        return {"display": f"Remote Desktop to {address}", "signed_in": False}

    client = _find(_FREERDP)
    if client is None:
        raise RemoteError(
            "No RDP program is installed. Install FreeRDP, for example: "
            "sudo apt install freerdp3-x11"
        )
    if any(ch in username + password for ch in "\r\n"):
        raise RemoteError("The user or password in the vault contains a line break.")
    process = popen(rdp_command(client, address, username), stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # FreeRDP asks on stdin only for what the arguments did not give it.
        lines = ([] if username else [""]) + [password]
        process.stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
        process.stdin.close()
    except OSError:
        pass
    return {"display": f"RDP to {username + '@' if username else ''}{address}",
            "signed_in": bool(password)}


def open_rustdesk(device: dict, popen=subprocess.Popen) -> dict:
    target = rustdesk_id(device)
    if not target:
        raise RemoteError("This entry has no rustdesk_id field. Add one in amvault with the machine's RustDesk ID.")
    client = _find(_RUSTDESK)
    if client is None:
        raise RemoteError("RustDesk is not installed on this computer. Get it from rustdesk.com.")
    popen([client, "--connect", target], stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"display": f"RustDesk to {target}", "signed_in": False}
