"""What to ask each kind of device, and how to read the answer.

One entry per platform: the commands to run and a parser that turns their
output into the same shape, so the monitor and the window never care what is
on the other end.

    {
      "cpu": 12.5 | None,              percent, when the device reports one
      "cpu_ticks": (busy, total) | None,   counters, when it reports those
      "mem_used": bytes | None, "mem_total": bytes | None,
      "uptime": seconds | None,
      "ifaces": [{"name", "up", "main",
                  "rx_bytes", "tx_bytes"   counters, or
                  "rx_bps", "tx_bps"}]     rates the device worked out itself
    }

`main` marks the interfaces whose traffic adds up to the device's total, so a
bridge and its member ports are not counted twice.

`finish()` turns two consecutive readings into rates.  Parsers are pure
functions of text so they can be tested against captured output; nothing here
opens a connection.
"""

from __future__ import annotations

import re

from . import powershell

SEPARATOR = "@@nettool@@"


class Collector:
    """`shell` is True for devices that close the connection after one exec
    request and so have to be driven through an interactive session."""

    def __init__(self, commands, parse, shell=False, every=1):
        self.commands = commands
        self.parse = parse
        self.shell = shell
        # Collect on every Nth tick; slow management planes get asked less.
        self.every = every


# --- Linux ----------------------------------------------------------------
# One round trip, wrapped in sh so the user's login shell (fish, csh) does not
# have to understand it.  No single quotes inside: it is passed as sh -c '...'.
_LINUX_SCRIPT = (
    "head -n1 /proc/stat; echo {s}; cat /proc/meminfo; echo {s}; cat /proc/net/dev; "
    "echo {s}; cat /proc/uptime; echo {s}; "
    "for i in /sys/class/net/*; do echo \"${{i##*/}} $(cat $i/operstate 2>/dev/null)\"; done; "
    "echo {s}; ip route show default 2>/dev/null"
).format(s=SEPARATOR)
LINUX_COMMAND = f"sh -c '{_LINUX_SCRIPT}'"


def parse_linux(outputs: list[str]) -> dict:
    parts = [part.strip() for part in outputs[0].split(SEPARATOR)]
    parts += [""] * (6 - len(parts))
    stat, meminfo, netdev, uptime, states, route = parts[:6]

    cpu_ticks = None
    numbers = [int(n) for n in stat.split()[1:] if n.isdigit()]
    if len(numbers) >= 4:
        # idle + iowait is time the CPU had nothing to run.
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        cpu_ticks = (sum(numbers) - idle, sum(numbers))

    mem = {}
    for line in meminfo.splitlines():
        key, _, rest = line.partition(":")
        value = rest.split()
        if value and value[0].isdigit():
            mem[key] = int(value[0]) * 1024
    total = mem.get("MemTotal")
    # MemAvailable counts reclaimable cache as free, which is what matters;
    # MemFree alone makes every healthy server look full.
    available = mem.get("MemAvailable", mem.get("MemFree"))

    operstate = dict(line.split()[:2] for line in states.splitlines() if len(line.split()) >= 2)
    default = re.search(r"\bdev\s+(\S+)", route)
    default_name = default.group(1) if default else None

    ifaces = []
    for line in netdev.splitlines():
        name, colon, rest = line.partition(":")
        fields = rest.split()
        if not colon or len(fields) < 9 or not fields[0].isdigit():
            continue
        name = name.strip()
        if name == "lo":
            continue
        ifaces.append({
            "name": name,
            # "unknown" is what tunnels and some virtual NICs report while working.
            "up": operstate.get(name, "unknown") in ("up", "unknown"),
            "main": name == default_name,
            "rx_bytes": int(fields[0]),
            "tx_bytes": int(fields[8]),
        })
    if ifaces and not any(i["main"] for i in ifaces):
        max(ifaces, key=lambda i: i["rx_bytes"] + i["tx_bytes"])["main"] = True

    seconds = uptime.split()
    return {
        "cpu": None,
        "cpu_ticks": cpu_ticks,
        "mem_used": total - available if total and available is not None else None,
        "mem_total": total,
        "uptime": float(seconds[0]) if seconds and _is_number(seconds[0]) else None,
        "ifaces": ifaces,
    }


# --- MikroTik RouterOS ----------------------------------------------------
# Scripted `get`s rather than `print`: print's columns, units and digit
# grouping change between RouterOS versions, while get returns bare values.
MIKROTIK_COMMAND = (
    ':put ("res,".[/system resource get cpu-load].",".[/system resource get free-memory].",".'
    '[/system resource get total-memory].",".[/system resource get uptime]); '
    ':foreach i in=[/interface find] do={:put ("if,".[/interface get $i name].",".'
    '[/interface get $i type].",".[/interface get $i running].",".'
    '[/interface get $i rx-byte].",".[/interface get $i tx-byte])}'
)

_ROS_PHYSICAL = ("ether", "wlan", "wifi", "lte", "sfp")


def parse_routeros_time(text: str) -> float | None:
    """`1w2d03:04:05` (scripts) or `1w2d3h4m5s` (print) -> seconds."""
    text = text.strip()
    match = re.fullmatch(
        r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+):(\d+):(\d+)|(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)", text
    )
    if not match or not text:
        return None
    w, d, h1, m1, s1, h2, m2, s2 = (int(g) if g else 0 for g in match.groups())
    return float(w * 604800 + d * 86400 + (h1 + h2) * 3600 + (m1 + m2) * 60 + s1 + s2)


def parse_mikrotik(outputs: list[str]) -> dict:
    result = {"cpu": None, "cpu_ticks": None, "mem_used": None, "mem_total": None,
              "uptime": None, "ifaces": []}
    for line in outputs[0].splitlines():
        line = line.strip()
        if line.startswith("res,"):
            fields = line.split(",")
            if len(fields) >= 5 and fields[1].isdigit():
                result["cpu"] = float(fields[1])
                if fields[2].isdigit() and fields[3].isdigit():
                    result["mem_total"] = int(fields[3])
                    result["mem_used"] = int(fields[3]) - int(fields[2])
                result["uptime"] = parse_routeros_time(fields[4])
        elif line.startswith("if,"):
            # Split from the right: an interface name may itself hold a comma.
            head, kind, running, rx, tx = line[3:].rsplit(",", 4)
            if rx.isdigit() and tx.isdigit():
                result["ifaces"].append({
                    "name": head, "up": running == "true",
                    "main": kind.startswith(_ROS_PHYSICAL),
                    "rx_bytes": int(rx), "tx_bytes": int(tx),
                })
    return result


# --- Cisco IOS / IOS-XE ---------------------------------------------------
CISCO_COMMANDS = [
    "show processes cpu | include CPU utilization",
    "show memory statistics | include Processor",
    "show version | include uptime",
    "show interfaces",
]

_CISCO_UNITS = {"year": 31536000, "week": 604800, "day": 86400, "hour": 3600, "minute": 60}
_CISCO_VIRTUAL = ("vlan", "loopback", "null", "tunnel", "port-channel", "bvi", "nve", "virtual")


def parse_cisco(outputs: list[str]) -> dict:
    outputs = list(outputs) + [""] * (4 - len(outputs))
    cpu_text, mem_text, version_text, if_text = outputs[:4]

    # "CPU utilization for five seconds: 7%/0%; one minute: 6%; five minutes: 5%"
    cpu = re.search(r"five seconds:\s*(\d+)%", cpu_text)

    # "Processor   <head>   <total>   <used>   <free> ..."  (head is hex)
    mem_used = mem_total = None
    mem = re.search(r"Processor\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)", mem_text)
    if mem:
        mem_total, mem_used = int(mem.group(1)), int(mem.group(2))

    uptime = None
    up = re.search(r"uptime is (.+)", version_text)
    if up:
        uptime = float(sum(
            int(count) * _CISCO_UNITS[unit]
            for count, unit in re.findall(r"(\d+) (year|week|day|hour|minute)s?", up.group(1))
        ))

    ifaces = []
    current = None
    for line in if_text.splitlines():
        header = re.match(r"^(\S+) is (.+?), line protocol is (\S+)", line)
        if header:
            name = header.group(1)
            current = {
                "name": name,
                "up": header.group(3).rstrip(",") == "up",
                "main": not name.lower().startswith(_CISCO_VIRTUAL),
                "rx_bps": 0.0, "tx_bps": 0.0,
            }
            ifaces.append(current)
            continue
        # "  5 minute input rate 1000 bits/sec, 2 packets/sec" (or "30 second ...")
        rate = re.search(r"(input|output) rate (\d+) bits/sec", line)
        if rate and current is not None:
            current["rx_bps" if rate.group(1) == "input" else "tx_bps"] = float(rate.group(2))

    return {"cpu": float(cpu.group(1)) if cpu else None, "cpu_ticks": None,
            "mem_used": mem_used, "mem_total": mem_total, "uptime": uptime, "ifaces": ifaces}


# --- Windows (over its OpenSSH server) ---------------------------------------
_WINDOWS_SCRIPT = (
    "$os=Get-CimInstance Win32_OperatingSystem;"
    "$cpu=(Get-CimInstance Win32_Processor|Measure-Object LoadPercentage -Average).Average;"
    "$up=[int]((Get-Date)-$os.LastBootUpTime).TotalSeconds;"
    "$ad=@(Get-NetAdapter|ForEach-Object{$s=$_|Get-NetAdapterStatistics -ErrorAction SilentlyContinue;"
    "@{name=$_.Name;status=[string]$_.Status;virtual=[bool]$_.Virtual;"
    "rx=[double]$s.ReceivedBytes;tx=[double]$s.SentBytes}});"
    "ConvertTo-Json -Compress -Depth 4 -InputObject @{cpu=$cpu;"
    "total=[double]$os.TotalVisibleMemorySize;free=[double]$os.FreePhysicalMemory;"
    "uptime=$up;adapters=$ad}"
)
WINDOWS_COMMAND = powershell.command(_WINDOWS_SCRIPT)


def parse_windows(outputs: list[str]) -> dict:
    data = powershell.parse_json(outputs[0])
    if not isinstance(data, dict):
        data = {}

    def number(key):
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    total, free = number("total"), number("free")      # reported in KiB
    adapters = data.get("adapters")
    if isinstance(adapters, dict):                      # one adapter is not wrapped in a list
        adapters = [adapters]
    ifaces = []
    for adapter in adapters or []:
        if not isinstance(adapter, dict) or not adapter.get("name"):
            continue
        ifaces.append({
            "name": str(adapter["name"]),
            "up": adapter.get("status") == "Up",
            # Hyper-V and VPN adapters carry traffic the physical one already counted.
            "main": not adapter.get("virtual"),
            "rx_bytes": int(adapter.get("rx") or 0),
            "tx_bytes": int(adapter.get("tx") or 0),
        })
    return {
        "cpu": number("cpu"), "cpu_ticks": None,
        "mem_used": int((total - free) * 1024) if total is not None and free is not None else None,
        "mem_total": int(total * 1024) if total is not None else None,
        "uptime": number("uptime"), "ifaces": ifaces,
    }


COLLECTORS = {
    # Starting PowerShell costs a second or two each time, so it is asked less often.
    "windows": Collector([WINDOWS_COMMAND], parse_windows, every=3),
    "linux": Collector([LINUX_COMMAND], parse_linux),
    "mikrotik": Collector([MIKROTIK_COMMAND], parse_mikrotik, every=2),
    "cisco_ios": Collector(CISCO_COMMANDS, parse_cisco, shell=True, every=3),
}


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def finish(previous: dict | None, current: dict, seconds: float) -> dict:
    """Turn a raw reading into what gets shown, using the one before it.

    Counters become rates; a counter that went backwards (a reboot, or a
    32-bit wrap) yields no rate for that interval rather than a wild spike.
    """
    cpu = current.get("cpu")
    ticks, before = current.get("cpu_ticks"), (previous or {}).get("cpu_ticks")
    if cpu is None and ticks and before and ticks[1] > before[1]:
        cpu = round(100.0 * max(0, ticks[0] - before[0]) / (ticks[1] - before[1]), 1)

    old = {i["name"]: i for i in (previous or {}).get("ifaces", [])}
    ifaces = []
    for iface in current.get("ifaces", []):
        rx, tx = iface.get("rx_bps"), iface.get("tx_bps")
        prior = old.get(iface["name"])
        if rx is None and prior and seconds > 0 and "rx_bytes" in iface and "rx_bytes" in prior:
            d_rx = iface["rx_bytes"] - prior["rx_bytes"]
            d_tx = iface["tx_bytes"] - prior["tx_bytes"]
            if d_rx >= 0 and d_tx >= 0:
                rx, tx = d_rx * 8 / seconds, d_tx * 8 / seconds
        ifaces.append({"name": iface["name"], "up": iface["up"], "main": iface["main"],
                       "rx_bps": rx, "tx_bps": tx})

    main = [i for i in ifaces if i["main"] and i["rx_bps"] is not None]
    return {
        "cpu": cpu,
        "mem_used": current.get("mem_used"),
        "mem_total": current.get("mem_total"),
        "uptime": current.get("uptime"),
        "rx_bps": sum(i["rx_bps"] for i in main) if main else None,
        "tx_bps": sum(i["tx_bps"] for i in main) if main else None,
        "ifaces": ifaces,
    }
