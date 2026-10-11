"""Reach hosts and find them: ping, the neighbour table, and a subnet scan.

Everything here uses what an ordinary user account can do -- the system's own
`ping`, the kernel's neighbour (ARP) table, and plain TCP connects -- so the
tool never needs to run as root or Administrator.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# The largest range one scan will sweep (a /22). Bigger than this is rarely
# one broadcast domain, and a slip of the finger should not ping a /8.
MAX_SCAN_ADDRESSES = 1024
PING_TIMEOUT_SECONDS = 1
PORT_TIMEOUT_SECONDS = 0.5
WORKERS = 96

# Ports that say what a host is for.  Deliberately few: this is a hint for a
# person looking at a list, not a port scan.
SERVICE_PORTS = {
    22: "SSH",
    23: "Telnet",
    80: "HTTP",
    443: "HTTPS",
    445: "SMB",
    3389: "RDP",
    8291: "Winbox",
}

_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$")
_PING_TIME = re.compile(r"time[=<]\s*([0-9.]+)\s*ms", re.IGNORECASE)
_MAC = re.compile(r"(?i)\b([0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2}){5})\b")
_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


class NetError(Exception):
    pass


def _hidden() -> dict:
    """Keep helper programs from flashing a console window on Windows."""
    if IS_WINDOWS:
        return {"creationflags": 0x0800_0000}  # CREATE_NO_WINDOW
    return {}


def safe_target(address: str) -> str:
    """Return `address` if it is an IP or a plain hostname, else raise.

    The value ends up as an argument to `ping`, and it comes from a vault field
    a person typed.  Anything starting with "-" would be read as an option.
    """
    address = (address or "").strip()
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        pass
    if _HOSTNAME.match(address):
        return address
    raise NetError(f"Not an address: {address!r}")


# --- ping ---------------------------------------------------------------
def parse_ping(output: str, returncode: int, elapsed_ms: float) -> dict:
    """Turn `ping`'s output into {"up", "ms"}.

    Windows exits 0 even when a router answers "Destination host unreachable",
    so there a reply only counts if it carries a TTL.
    """
    up = returncode == 0
    if IS_WINDOWS:
        up = up and "ttl=" in output.lower()
    if not up:
        return {"up": False, "ms": None}
    match = _PING_TIME.search(output)
    # A localised ping may word "time" differently; the wall clock is a fair
    # stand-in, if a little generous.
    ms = float(match.group(1)) if match else elapsed_ms
    return {"up": True, "ms": round(ms, 1)}


def ping(address: str) -> dict:
    try:
        target = safe_target(address)
    except NetError:
        return {"up": False, "ms": None}
    if IS_WINDOWS:
        command = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_SECONDS * 1000), target]
    elif IS_MAC:
        command = ["ping", "-n", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS * 1000), target]
    else:
        command = ["ping", "-n", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), target]
    started = time.monotonic()
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=PING_TIMEOUT_SECONDS + 3, **_hidden(),
        )
    except (OSError, subprocess.SubprocessError):
        return {"up": False, "ms": None}
    return parse_ping(done.stdout, done.returncode, (time.monotonic() - started) * 1000)


# --- local networks -----------------------------------------------------
def parse_ip_json(text: str) -> list[dict]:
    """Networks from `ip -j -4 addr`."""
    networks = []
    for link in json.loads(text or "[]"):
        if "LOOPBACK" in (link.get("flags") or []):
            continue
        for info in link.get("addr_info") or []:
            if info.get("family") != "inet":
                continue
            interface = ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}")
            networks.append({
                "cidr": str(interface.network),
                "address": str(interface.ip),
                "interface": link.get("ifname") or "",
            })
    return networks


def _is_physical(interface: str) -> bool:
    """Whether a Linux interface is real hardware rather than a bridge or tunnel.

    Only hardware has a `device` link in sysfs.  A machine running containers
    or a VPN has a dozen virtual networks, and none of them is the LAN the
    person wants to look at.
    """
    return os.path.exists(f"/sys/class/net/{interface}/device")


def _suggest_range(network: dict) -> str:
    """The range to scan for a network: all of it, or the /24 we sit in."""
    whole = ipaddress.ip_network(network["cidr"])
    if whole.num_addresses <= MAX_SCAN_ADDRESSES:
        return str(whole)
    return str(ipaddress.ip_network(f"{network['address']}/24", strict=False))


def _primary_address() -> str | None:
    """The address this machine uses to reach the wider network.

    Connecting a UDP socket sends nothing; it only makes the kernel choose a
    source address, which is the one we want.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def local_networks() -> list[dict]:
    """IPv4 networks this machine is directly attached to."""
    networks: list[dict] = []
    try:
        if IS_WINDOWS:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | "
                 "Select-Object IPAddress,PrefixLength,InterfaceAlias | ConvertTo-Json"],
                capture_output=True, text=True, timeout=15, **_hidden(),
            )
            rows = json.loads(done.stdout or "[]")
            for row in rows if isinstance(rows, list) else [rows]:
                interface = ipaddress.ip_interface(f"{row['IPAddress']}/{row['PrefixLength']}")
                if interface.ip.is_loopback or interface.ip.is_link_local:
                    continue
                networks.append({"cidr": str(interface.network), "address": str(interface.ip),
                                 "interface": row.get("InterfaceAlias") or ""})
        elif not IS_MAC:
            done = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True,
                                  text=True, timeout=10)
            networks = parse_ip_json(done.stdout)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        networks = []

    if not networks:
        # No way to learn the mask; a /24 around our own address is the usual
        # answer and a safe size.
        address = _primary_address()
        if address:
            network = ipaddress.ip_network(f"{address}/24", strict=False)
            networks = [{"cidr": str(network), "address": address, "interface": ""}]

    primary = _primary_address()
    for network in networks:
        # Off Linux there is no cheap test, so everything counts as physical
        # and the routing table's choice breaks the tie.
        network["physical"] = (
            _is_physical(network["interface"]) if sys.platform.startswith("linux") else True
        )
        network["scan"] = _suggest_range(network)
    networks.sort(key=lambda n: (not n["physical"], n["address"] != primary, n["cidr"]))
    return networks


def scan_range(cidr: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network((cidr or "").strip(), strict=False)
    except ValueError as exc:
        raise NetError(f"Not a network range: {cidr!r}. Write it like 192.168.1.0/24.") from exc
    if network.version != 4:
        raise NetError("Only IPv4 ranges can be scanned.")
    if network.num_addresses > MAX_SCAN_ADDRESSES:
        raise NetError(
            f"{network} has {network.num_addresses} addresses; the most one scan "
            f"covers is {MAX_SCAN_ADDRESSES} (a /22). Scan it in smaller parts."
        )
    return network


# --- neighbour table ----------------------------------------------------
def normalise_mac(mac: str) -> str:
    return ":".join(part.zfill(2) for part in re.split(r"[:-]", mac.lower()))


def parse_neighbours(text: str) -> dict[str, str]:
    """address -> MAC, from `ip neigh`, `arp -a` (Windows or BSD) output.

    Lines without a MAC (FAILED, incomplete) and broadcast/multicast entries
    are skipped, so only real stations come back.
    """
    table: dict[str, str] = {}
    for line in text.splitlines():
        ip_match, mac_match = _IPV4.search(line), _MAC.search(line)
        if not ip_match or not mac_match:
            continue
        mac = normalise_mac(mac_match.group(1))
        if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or mac.startswith("01:00:5e"):
            continue
        table[ip_match.group(1)] = mac
    return table


def neighbours() -> dict[str, str]:
    command = ["arp", "-a"] if (IS_WINDOWS or IS_MAC) else ["ip", "-4", "neigh"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=10, **_hidden())
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_neighbours(done.stdout)


# --- what a host is -----------------------------------------------------
def open_ports(address: str, ports=SERVICE_PORTS) -> list[int]:
    found = []
    for port in ports:
        try:
            with socket.create_connection((address, port), timeout=PORT_TIMEOUT_SECONDS):
                found.append(port)
        except OSError:
            continue
    return found


def ssh_banner(address: str, port: int = 22, timeout: float = 1.2) -> str:
    """The line an SSH server introduces itself with, e.g. `SSH-2.0-ROSSSH`.

    It is sent before any sign-in, so it identifies the software without
    credentials -- and without anything being sent to the server at all.
    """
    try:
        target = safe_target(address)
        with socket.create_connection((target, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            data = connection.recv(256)
    except (NetError, OSError):
        return ""
    line = data.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    return line if line.startswith("SSH-") else ""


def hostname_of(address: str) -> str:
    try:
        name = socket.gethostbyaddr(address)[0]
    except OSError:
        return ""
    return "" if name == address else name


def guess_platform(ports: list[int]) -> str:
    """A cautious guess from the open ports; "" when they do not settle it.

    Winbox is MikroTik's own management port and RDP is Windows'.  SSH alone
    says nothing -- routers, switches and servers all speak it.
    """
    if 8291 in ports:
        return "mikrotik"
    if 3389 in ports:
        return "windows"
    return ""


class Scanner:
    """One subnet scan at a time, reporting what it finds as it goes.

    `emit` receives:
        {"event": "scan.host", "host": {...}}       found, or learned more about
        {"event": "scan.progress", "done", "total"}
        {"event": "scan.done", "cancelled": bool, "found": int}
    """

    def __init__(self, emit):
        self._emit = emit
        self._lock = threading.Lock()
        self._cancel: threading.Event | None = None

    def start(self, cidr: str) -> dict:
        network = scan_range(cidr)
        addresses = [str(a) for a in (network.hosts() if network.prefixlen < 31 else network)]
        with self._lock:
            if self._cancel is not None and not self._cancel.is_set():
                raise NetError("A scan is already running.")
            cancel = self._cancel = threading.Event()
        threading.Thread(target=self._run, args=(addresses, cancel), daemon=True).start()
        return {"cidr": str(network), "total": len(addresses)}

    def cancel(self) -> dict:
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
        return {}

    def _run(self, addresses: list[str], cancel: threading.Event) -> None:
        found: dict[str, dict] = {}
        found_lock = threading.Lock()
        done = 0

        def describe(address: str, ms) -> None:
            if cancel.is_set():
                return
            ports = open_ports(address)
            host = {
                "address": address, "ms": ms, "mac": "",
                "hostname": hostname_of(address),
                "ports": ports, "platform": guess_platform(ports),
            }
            with found_lock:
                found[address] = host
            self._emit({"event": "scan.host", "host": host})

        def sweep(address: str) -> None:
            nonlocal done
            if not cancel.is_set():
                result = ping(address)
                if result["up"]:
                    describe(address, result["ms"])
            with found_lock:
                done += 1
                # Every address would flood the window; every few is smooth enough.
                if done % 8 == 0 or done == len(addresses):
                    self._emit({"event": "scan.progress", "done": done, "total": len(addresses)})

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(sweep, addresses))

            # Hosts that drop ping still had to answer ARP for us to try, so
            # the neighbour table catches them -- and gives every host its MAC.
            if not cancel.is_set():
                wanted = set(addresses)
                table = {ip: mac for ip, mac in neighbours().items() if ip in wanted}
                silent = [ip for ip in table if ip not in found]
                list(pool.map(lambda ip: describe(ip, None), silent))
                for address, mac in table.items():
                    host = found.get(address)
                    if host is not None:
                        host["mac"] = mac
                        self._emit({"event": "scan.host", "host": host})

        self._emit({"event": "scan.done", "cancelled": cancel.is_set(), "found": len(found)})
        cancel.set()
