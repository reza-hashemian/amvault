"""Manage a device: read its configuration as tables, change it deliberately.

A panel is a live SSH session to one device plus knowledge of its platform:
which sections there are, how to read each, and which changes are offered.

Changes follow one rule.  The window never sends commands; it names an action
and its values.  `plan()` turns that into the exact commands and hands them
back to be shown, and `apply()` builds them again from the same inputs before
running them -- so what runs is always what this module would generate, every
value has been validated here, and nothing can be smuggled in from the window.

A section reads as:

    {"columns": [{"key", "label", "mono"?}],
     "rows": [{"id", "cells": {key: text}}],
     "actions": [{"id", "label", "scope": "row" | "section",
                  "fields": [{"name", "label", "type", "from"?}],
                  "when"?: {column: value}}]}
"""

from __future__ import annotations

import re

from . import powershell
from .collectors import parse_routeros_time
from .sshclient import Session

MAX_TEXT = 200


class PanelError(Exception):
    """A request the panel refuses; the message is shown to the person."""


def _text(value, label: str, limit: int = MAX_TEXT) -> str:
    """A free-text value that is safe to place on a command line."""
    value = str(value or "").strip()
    if len(value) > limit:
        raise PanelError(f"{label} is too long (at most {limit} characters).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PanelError(f"{label} cannot contain line breaks or control characters.")
    return value


def _vlan_id(value) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise PanelError("The VLAN number must be a whole number.") from None
    if not 1 <= number <= 4094:
        raise PanelError("The VLAN number must be between 1 and 4094.")
    return number


def _column(key, label, mono=False):
    return {"key": key, "label": label, "mono": mono}


class Panel:
    platform = ""
    sections: list[dict] = []

    def __init__(self, session: Session):
        self.session = session
        self.can_change = True
        self.note = ""
        # Row ids seen in the last read of each section: an action may only
        # name something the device itself listed.
        self._seen: dict[str, dict[str, dict]] = {}

    def describe(self) -> dict:
        return {"platform": self.platform, "sections": self.sections,
                "can_change": self.can_change, "note": self.note}

    def read(self, section: str) -> dict:
        reader = getattr(self, "read_" + section, None)
        if reader is None or section not in {s["id"] for s in self.sections}:
            raise PanelError(f"Unknown section: {section}")
        result = reader()
        self._seen[section] = {row["id"]: row["cells"] for row in result["rows"]}
        if not self.can_change:
            result["actions"] = []
        return result

    def _row(self, section: str, row_id) -> dict:
        cells = self._seen.get(section, {}).get(str(row_id))
        if cells is None:
            raise PanelError("That row is no longer in the list. Reload and try again.")
        return cells

    def plan(self, section: str, action: str, row, values: dict) -> dict:
        if not self.can_change:
            raise PanelError(self.note or "Changes are not available on this session.")
        planner = getattr(self, f"plan_{section}_{action}", None)
        if planner is None:
            raise PanelError(f"Unknown action: {action}")
        return planner(row, values if isinstance(values, dict) else {})

    def apply(self, section: str, action: str, row, values: dict) -> dict:
        plan = self.plan(section, action, row, values)
        return self.run_changes(plan["commands"], plan.get("tolerate", ()))

    def run_changes(self, commands: list[str], tolerate=()) -> dict:
        """Run planned commands.  A rejection of a command in `tolerate` is
        expected on some models and does not stop the rest."""
        raise NotImplementedError

    def close(self) -> None:
        self.session.close()


# --- MikroTik RouterOS ------------------------------------------------------
def parse_as_value(text: str) -> list[dict]:
    """Records from `:foreach i in=[<menu> print as-value] do={:put $i}`.

    Each line is `key=value;key=value;...`.  A value may itself contain `;`
    (a comment, say), so a piece only starts a new pair when it looks like
    `name=`; otherwise it belongs to the value before it.
    """
    records = []
    for line in text.replace("\r", "").split("\n"):
        if "=" not in line:
            continue
        record: dict[str, str] = {}
        key = None
        for piece in line.split(";"):
            match = re.match(r"^([.\w-]+)=(.*)$", piece, re.S)
            if match:
                key = match.group(1)
                record[key] = match.group(2)
            elif key is not None:
                record[key] += ";" + piece
        if record:
            records.append(record)
    return records


def ros_quote(value: str) -> str:
    """A RouterOS double-quoted string: `$` would otherwise start a variable."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _yes(record: dict, key: str) -> bool:
    return record.get(key) == "true"


class MikrotikPanel(Panel):
    platform = "mikrotik"
    sections = [
        {"id": "overview", "label": "Overview"},
        {"id": "interfaces", "label": "Interfaces"},
        {"id": "addresses", "label": "IP addresses"},
        {"id": "routes", "label": "Routes"},
        {"id": "leases", "label": "DHCP leases"},
        {"id": "firewall", "label": "Firewall"},
        {"id": "services", "label": "Services"},
    ]

    def _records(self, menu: str) -> list[dict]:
        return parse_as_value(self.session.run(f":foreach i in=[{menu} print as-value] do={{:put $i}}"))

    def _state(self, record: dict) -> str:
        if _yes(record, "disabled"):
            return "Disabled"
        if "running" in record:
            return "Running" if _yes(record, "running") else "Not running"
        if "active" in record:
            return "Active" if _yes(record, "active") else "Inactive"
        if "invalid" in record and _yes(record, "invalid"):
            return "Invalid"
        return "Enabled"

    def _table(self, records, columns, row_id=lambda r: r.get(".id", ""), actions=()):
        rows = []
        for record in records:
            cells = {c["key"]: record.get(c["key"], "") for c in columns}
            if "state" in cells:
                cells["state"] = self._state(record)
            rows.append({"id": row_id(record), "cells": cells})
        return {"columns": columns, "rows": rows, "actions": list(actions)}

    def read_overview(self) -> dict:
        names = ["version", "board-name", "architecture-name", "cpu-count", "cpu-load",
                 "free-memory", "total-memory", "free-hdd-space", "total-hdd-space", "uptime"]
        gets = '.";".'.join(f'"{n}=".[/system resource get {n}]' for n in names)
        text = self.session.run(f':put ("identity=".[/system identity get name].";".{gets})')
        record = (parse_as_value(text) or [{}])[0]

        def size(key):
            value = record.get(key, "")
            return f"{int(value) / 1048576:.0f} MiB" if value.isdigit() else value

        seconds = parse_routeros_time(record.get("uptime", ""))
        shown = [
            ("Name", record.get("identity", "")),
            ("RouterOS", record.get("version", "")),
            ("Board", record.get("board-name", "")),
            ("Architecture", record.get("architecture-name", "")),
            ("CPU", f"{record.get('cpu-load', '?')}% of {record.get('cpu-count', '?')} cores"),
            ("Memory free", f"{size('free-memory')} of {size('total-memory')}"),
            ("Storage free", f"{size('free-hdd-space')} of {size('total-hdd-space')}"),
            ("Uptime", record.get("uptime", "") if seconds is None else _days(seconds)),
        ]
        return {
            "columns": [_column("item", "Item"), _column("value", "Value")],
            "rows": [{"id": label, "cells": {"item": label, "value": value}} for label, value in shown],
            "actions": [],
        }

    def read_interfaces(self) -> dict:
        columns = [_column("name", "Name", True), _column("type", "Type"), _column("state", "State"),
                   _column("mtu", "MTU"), _column("mac-address", "MAC", True),
                   _column("comment", "Comment")]
        return self._table(self._records("/interface"), columns, row_id=lambda r: r.get("name", ""), actions=[
            {"id": "comment", "label": "Edit comment", "scope": "row",
             "fields": [{"name": "comment", "label": "Comment", "type": "text", "from": "comment"}]},
            {"id": "disable", "label": "Disable", "scope": "row", "fields": [],
             "unless": {"state": "Disabled"}},
            {"id": "enable", "label": "Enable", "scope": "row", "fields": [],
             "when": {"state": "Disabled"}},
        ])

    def read_addresses(self) -> dict:
        return self._table(self._records("/ip address"), [
            _column("address", "Address", True), _column("network", "Network", True),
            _column("interface", "Interface", True), _column("state", "State"),
            _column("comment", "Comment")])

    def read_routes(self) -> dict:
        return self._table(self._records("/ip route"), [
            _column("dst-address", "Destination", True), _column("gateway", "Gateway", True),
            _column("distance", "Distance"), _column("state", "State"), _column("comment", "Comment")])

    def read_leases(self) -> dict:
        return self._table(self._records("/ip dhcp-server lease"), [
            _column("address", "Address", True), _column("mac-address", "MAC", True),
            _column("host-name", "Host name"), _column("server", "Server"),
            _column("status", "Status"), _column("comment", "Comment")])

    def read_firewall(self) -> dict:
        return self._table(self._records("/ip firewall filter"), [
            _column("chain", "Chain"), _column("action", "Action"), _column("protocol", "Protocol"),
            _column("src-address", "Source", True), _column("dst-address", "Destination", True),
            _column("dst-port", "Port", True), _column("in-interface", "In interface", True),
            _column("state", "State"), _column("comment", "Comment")])

    def read_services(self) -> dict:
        return self._table(self._records("/ip service"), [
            _column("name", "Service"), _column("port", "Port", True),
            _column("address", "Allowed from", True), _column("state", "State")])

    # -- changes
    def _interface(self, row) -> str:
        return ros_quote(self._row("interfaces", row)["name"])

    def plan_interfaces_comment(self, row, values) -> dict:
        comment = _text(values.get("comment"), "The comment")
        name = self._row("interfaces", row)["name"]
        return {"title": f"Set the comment on {name}",
                "commands": [f"/interface set [find name={self._interface(row)}] comment={ros_quote(comment)}"],
                "warning": ""}

    def plan_interfaces_disable(self, row, values) -> dict:
        name = self._row("interfaces", row)["name"]
        return {"title": f"Disable {name}",
                "commands": [f"/interface disable [find name={self._interface(row)}]"],
                "warning": "Traffic through this interface stops at once. If it carries your "
                           "connection to the router, this session ends and you will need "
                           "another way in to enable it again."}

    def plan_interfaces_enable(self, row, values) -> dict:
        name = self._row("interfaces", row)["name"]
        return {"title": f"Enable {name}",
                "commands": [f"/interface enable [find name={self._interface(row)}]"], "warning": ""}

    def run_changes(self, commands: list[str], tolerate=()) -> dict:
        # RouterOS applies and saves a change in one step; it prints nothing
        # on success and the reason on failure.
        output = "\n".join(self.session.run(command).strip() for command in commands).strip()
        return {"ok": output == "", "output": output}


def _days(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days} days, {hours} hours, {rest // 60} minutes"


# --- Cisco IOS ----------------------------------------------------------------
def parse_interfaces_status(text: str) -> list[dict]:
    """`show interfaces status`, read by the header's column positions --
    the Name column may be empty or contain spaces, so splitting on
    whitespace would misplace everything after it."""
    lines = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    header = next((i for i, line in enumerate(lines) if line.startswith("Port") and "Status" in line), None)
    if header is None:
        return []
    head = lines[header]
    name_at, status_at, vlan_at, duplex_at = (head.index(w) for w in ("Name", "Status", "Vlan", "Duplex"))
    rows = []
    for line in lines[header + 1:]:
        if len(line) < duplex_at or line.startswith("Port"):
            continue
        rest = line[duplex_at:].split(None, 2)
        rows.append({
            "port": line[:name_at].strip(), "name": line[name_at:status_at].strip(),
            "status": line[status_at:vlan_at].strip(), "vlan": line[vlan_at:duplex_at].strip(),
            "duplex": rest[0] if rest else "", "speed": rest[1] if len(rest) > 1 else "",
            "type": rest[2].strip() if len(rest) > 2 else "",
        })
    return rows


def parse_trunks(text: str) -> list[dict]:
    """`show interfaces trunk`: several small tables, one after another, each
    starting with a "Port ..." heading.  The first gives mode and native VLAN,
    the second the allowed list; the rest (active, forwarding) are skipped."""
    ports: dict[str, dict] = {}
    table = ""
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("Port"):
            table = "summary" if "Mode" in line else "allowed" if "allowed on trunk" in line else ""
            continue
        fields = line.split()
        if not fields or not table:
            continue
        if table == "summary" and len(fields) >= 5:
            ports[fields[0]] = {"port": fields[0], "mode": fields[1], "encapsulation": fields[2],
                                "status": fields[3], "native": fields[4], "allowed": ""}
        elif table == "allowed" and fields[0] in ports:
            ports[fields[0]]["allowed"] = "".join(fields[1:])
    return list(ports.values())


def _vlan_list(value, required: bool) -> str:
    """A VLAN list as IOS writes it: `10,20,30-35`.  Returns it normalised."""
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        if required:
            raise PanelError("Give at least one VLAN number.")
        return ""
    if not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", text):
        raise PanelError("Write the VLANs as numbers and ranges, like 10,20,30-35.")
    for part in text.split(","):
        ends = [_vlan_id(number) for number in part.split("-")]
        if len(ends) == 2 and ends[0] >= ends[1]:
            raise PanelError(f"The range {part} runs backwards.")
    return text


_VLANS_FIELD = {"name": "vlans", "label": "VLANs", "type": "text"}
_TRUNK_FIELDS = [
    {"name": "vlans", "label": "Allowed VLANs", "type": "text"},
    {"name": "native", "label": "Native VLAN", "type": "number"},
]


def parse_ip_brief(text: str) -> list[dict]:
    rows = []
    for line in text.replace("\r", "").split("\n"):
        match = re.match(r"^(\S+)\s+(\S+)\s+(YES|NO)\s+(\S+)\s+(.+?)\s+(\S+)\s*$", line)
        if match:
            interface, address, _ok, method, status, protocol = match.groups()
            rows.append({"interface": interface, "address": address, "method": method,
                         "status": status, "protocol": protocol})
    return rows


def parse_vlan_brief(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.replace("\r", "").split("\n"):
        match = re.match(r"^(\d+)\s+(\S+)\s+(\S+)\s*(.*)$", line)
        if match:
            rows.append({"vlan": match.group(1), "name": match.group(2),
                         "status": match.group(3), "ports": match.group(4).strip()})
        elif rows and line.startswith(" ") and line.strip() and not line.strip().startswith("-"):
            # A long port list wraps onto lines that begin with spaces.
            rows[-1]["ports"] = (rows[-1]["ports"] + " " + line.strip()).strip()
    return rows


def parse_mac_table(text: str) -> list[dict]:
    rows = []
    for line in text.replace("\r", "").split("\n"):
        match = re.match(r"^\s*(\d+|All)\s+([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+(\S+)\s+(.+?)\s*$", line)
        if match:
            rows.append({"vlan": match.group(1), "mac": match.group(2).lower(),
                         "type": match.group(3).capitalize(), "port": match.group(4)})
    return rows


def parse_version(text: str) -> list[tuple[str, str]]:
    def find(pattern):
        match = re.search(pattern, text, re.M)
        return match.group(1).strip() if match else ""

    return [
        ("Name", find(r"^(\S+) uptime is")),
        ("Model", find(r"^[Cc]isco (\S+) .*(?:processor|bytes of memory)")),
        ("Software", find(r"Version ([^,\s]+)")),
        ("Uptime", find(r"uptime is (.+)$")),
        ("Serial number", find(r"(?:System [Ss]erial [Nn]umber\s*:|Processor board ID)\s*(\S+)")),
        ("Image", find(r'System image file is "([^"]+)"')),
        ("Last restart", find(r"(?:Last reload reason|System returned to ROM by)\s*:?\s*(.+)$")),
    ]


class CiscoPanel(Panel):
    platform = "cisco_ios"
    sections = [
        {"id": "overview", "label": "Overview"},
        {"id": "ports", "label": "Ports"},
        {"id": "vlans", "label": "VLANs"},
        {"id": "trunks", "label": "Trunks"},
        {"id": "ip", "label": "IP interfaces"},
        {"id": "mac", "label": "MAC table"},
    ]

    def __init__(self, session: Session, enable_secret: str | None = None):
        super().__init__(session)
        # Privileged mode is what a person types `enable` for; with the
        # secret in the vault it is entered for them.
        self.can_change = session.enable(enable_secret)
        if not self.can_change:
            self.note = (
                "Viewing only. This account starts in user mode and the vault entry has no "
                "working enable_password, so configuration changes are not offered."
            )

    def read_overview(self) -> dict:
        shown = parse_version(self.session.run_in_shell("show version"))
        shown.append(("Mode", "Privileged (#)" if self.can_change else "User (>)"))
        return {
            "columns": [_column("item", "Item"), _column("value", "Value")],
            "rows": [{"id": label, "cells": {"item": label, "value": value or "—"}} for label, value in shown],
            "actions": [{"id": "save", "label": "Save configuration", "scope": "section", "fields": []}],
        }

    def read_ports(self) -> dict:
        records = parse_interfaces_status(self.session.run_in_shell("show interfaces status"))
        columns = [_column("port", "Port", True), _column("name", "Description"),
                   _column("status", "Status"), _column("vlan", "VLAN"), _column("duplex", "Duplex"),
                   _column("speed", "Speed"), _column("type", "Type")]
        return {
            "columns": columns,
            "rows": [{"id": r["port"], "cells": r} for r in records],
            "actions": [
                {"id": "describe", "label": "Edit description", "scope": "row",
                 "fields": [{"name": "description", "label": "Description", "type": "text", "from": "name"}]},
                {"id": "vlan", "label": "Set access VLAN", "scope": "row",
                 "fields": [{"name": "vlan", "label": "VLAN number", "type": "number", "from": "vlan"}]},
                {"id": "trunk", "label": "Make trunk", "scope": "row", "fields": _TRUNK_FIELDS},
                {"id": "allow", "label": "Add VLANs to trunk", "scope": "row",
                 "fields": [_VLANS_FIELD], "when": {"vlan": "trunk"}},
                {"id": "disallow", "label": "Remove VLANs from trunk", "scope": "row",
                 "fields": [_VLANS_FIELD], "when": {"vlan": "trunk"}},
                {"id": "shutdown", "label": "Shut down", "scope": "row", "fields": [],
                 "unless": {"status": "disabled"}},
                {"id": "enable", "label": "Bring up", "scope": "row", "fields": [],
                 "when": {"status": "disabled"}},
            ],
        }

    def read_vlans(self) -> dict:
        records = parse_vlan_brief(self.session.run_in_shell("show vlan brief"))
        return {
            "columns": [_column("vlan", "VLAN", True), _column("name", "Name"),
                        _column("status", "Status"), _column("ports", "Ports", True)],
            "rows": [{"id": r["vlan"], "cells": r} for r in records],
            "actions": [{"id": "create", "label": "New VLAN", "scope": "section", "fields": [
                {"name": "vlan", "label": "VLAN number", "type": "number"},
                {"name": "name", "label": "Name", "type": "text"}]}],
        }

    def read_trunks(self) -> dict:
        records = parse_trunks(self.session.run_in_shell("show interfaces trunk"))
        return {
            "columns": [_column("port", "Port", True), _column("mode", "Mode"),
                        _column("encapsulation", "Encapsulation"), _column("status", "Status"),
                        _column("native", "Native VLAN"), _column("allowed", "Allowed VLANs", True)],
            "rows": [{"id": r["port"], "cells": r} for r in records], "actions": [],
        }

    def read_ip(self) -> dict:
        records = parse_ip_brief(self.session.run_in_shell("show ip interface brief"))
        return {
            "columns": [_column("interface", "Interface", True), _column("address", "Address", True),
                        _column("method", "Set by"), _column("status", "Status"),
                        _column("protocol", "Protocol")],
            "rows": [{"id": r["interface"], "cells": r} for r in records], "actions": [],
        }

    def read_mac(self) -> dict:
        records = parse_mac_table(self.session.run_in_shell("show mac address-table"))
        return {
            "columns": [_column("vlan", "VLAN", True), _column("mac", "MAC", True),
                        _column("type", "Type"), _column("port", "Port", True)],
            "rows": [{"id": f"{r['vlan']}/{r['mac']}", "cells": r} for r in records], "actions": [],
        }

    # -- changes
    _UNSAVED = ("This changes the running configuration only. Use Save configuration on the "
                "Overview tab to keep it after a restart.")

    def _port(self, row) -> str:
        port = self._row("ports", row)["port"]
        # Belt and braces: the name came from the device, but it is about to
        # be typed into its configuration prompt.
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9/.:-]*", port):
            raise PanelError(f"Unexpected port name: {port!r}")
        return port

    def _in_interface(self, row, *lines: str) -> list[str]:
        return ["configure terminal", f"interface {self._port(row)}", *lines, "end"]

    def plan_ports_describe(self, row, values) -> dict:
        description = _text(values.get("description"), "The description")
        if "?" in description:
            # Typed at an IOS prompt, "?" asks for help instead of being text.
            raise PanelError('The description cannot contain "?".')
        line = f"description {description}" if description else "no description"
        return {"title": f"Set the description of {self._port(row)}",
                "commands": self._in_interface(row, line), "warning": self._UNSAVED}

    def plan_ports_vlan(self, row, values) -> dict:
        vlan = _vlan_id(values.get("vlan"))
        cells = self._row("ports", row)
        warning = self._UNSAVED
        if cells.get("vlan") == "trunk":
            warning = ("This port is a trunk. Making it an access port stops every other VLAN "
                       "on this link, which usually disconnects the switch beyond it. " + warning)
        return {"title": f"Put {self._port(row)} in VLAN {vlan}",
                "commands": self._in_interface(row, "switchport mode access",
                                               f"switchport access vlan {vlan}"),
                "warning": warning}

    def plan_ports_trunk(self, row, values) -> dict:
        allowed = _vlan_list(values.get("vlans"), required=False)
        native = str(values.get("native") or "").strip()
        encapsulation = "switchport trunk encapsulation dot1q"
        lines = [encapsulation, "switchport mode trunk"]
        if allowed:
            lines.append(f"switchport trunk allowed vlan {allowed}")
        if native:
            lines.append(f"switchport trunk native vlan {_vlan_id(native)}")

        cells = self._row("ports", row)
        notes = []
        if cells.get("vlan") == "trunk":
            if allowed:
                notes.append("This replaces the whole list of allowed VLANs on a working trunk; any "
                             "VLAN not in the new list stops crossing this link. To change one VLAN, "
                             "use Add or Remove VLANs instead.")
        else:
            notes.append("A device that expects a plain access port (a PC, a printer) loses its "
                         "connection when its port becomes a trunk.")
        if not allowed:
            notes.append("With no list given, every VLAN is allowed across the trunk.")
        notes.append("Switches that only speak 802.1Q reject the encapsulation line; that is "
                     "expected and the remaining lines are still sent.")
        notes.append(self._UNSAVED)
        return {"title": f"Make {self._port(row)} a trunk",
                "commands": self._in_interface(row, *lines),
                "tolerate": [encapsulation], "warning": " ".join(notes)}

    def _trunk_change(self, row, values, verb: str, title: str, warning: str) -> dict:
        if self._row("ports", row).get("vlan") != "trunk":
            raise PanelError("This port is not a trunk.")
        vlans = _vlan_list(values.get("vlans"), required=True)
        return {"title": title.format(vlans=vlans, port=self._port(row)),
                "commands": self._in_interface(row, f"switchport trunk allowed vlan {verb} {vlans}"),
                "warning": (warning + " " + self._UNSAVED).strip()}

    def plan_ports_allow(self, row, values) -> dict:
        return self._trunk_change(row, values, "add", "Allow VLAN {vlans} on {port}", "")

    def plan_ports_disallow(self, row, values) -> dict:
        return self._trunk_change(
            row, values, "remove", "Stop VLAN {vlans} on {port}",
            "Devices in these VLANs on the far side of this link lose their connection. If the "
            "switch's own management VLAN is among them and you reach it through this link, "
            "this session ends.")

    def plan_ports_shutdown(self, row, values) -> dict:
        return {"title": f"Shut down {self._port(row)}",
                "commands": self._in_interface(row, "shutdown"),
                "warning": "Whatever is connected to this port loses its link at once. If your "
                           "own connection to the switch runs through it, this session ends. "
                           + self._UNSAVED}

    def plan_ports_enable(self, row, values) -> dict:
        return {"title": f"Bring up {self._port(row)}",
                "commands": self._in_interface(row, "no shutdown"), "warning": self._UNSAVED}

    def plan_vlans_create(self, row, values) -> dict:
        vlan = _vlan_id(values.get("vlan"))
        name = _text(values.get("name"), "The name", 32)
        if name and not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise PanelError("A VLAN name may use letters, digits, - and _ only.")
        lines = ["configure terminal", f"vlan {vlan}"] + ([f"name {name}"] if name else []) + ["end"]
        return {"title": f"Create VLAN {vlan}", "commands": lines, "warning": self._UNSAVED}

    def plan_overview_save(self, row, values) -> dict:
        return {"title": "Save the running configuration",
                # `write memory` rather than `copy running-config startup-config`:
                # the copy form stops to ask for a filename.
                "commands": ["write memory"],
                "warning": "Everything changed since the last save, by anyone, becomes the "
                           "configuration the device starts with."}

    def run_changes(self, commands: list[str], tolerate=()) -> dict:
        outputs = []
        ok = True
        for command in commands:
            output = self.session.run_in_shell(command).strip()
            rejected = any(_is_ios_error(line) for line in output.split("\n"))
            if rejected and command in tolerate:
                continue
            if output:
                outputs.append(f"{command}\n{output}")
            if rejected:
                ok = False
                break
        if not ok and self.session.prompt.rstrip().endswith(")#"):
            # Do not leave the session sitting in configuration mode.
            self.session.run_in_shell("end")
        return {"ok": ok, "output": "\n".join(outputs)}


# --- Linux ------------------------------------------------------------------
_SEP = "@@nettool@@"
# Read in one round trip, through sh so the account's login shell (fish, csh)
# does not have to understand it.  No single quotes inside.
_LINUX_OVERVIEW = (
    "sh -c 'hostname; echo {s}; . /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\"; echo {s}; "
    "uname -r; echo {s}; cat /proc/uptime; echo {s}; cat /proc/loadavg; echo {s}; nproc; echo {s}; "
    "grep -E \"^(MemTotal|MemAvailable):\" /proc/meminfo; echo {s}; id -un'"
).format(s=_SEP)

_SERVICE_ORDER = {"failed": 0, "running": 1, "exited": 2}


def parse_services(text: str) -> list[dict]:
    """`systemctl list-units --type=service --all --plain --no-legend`."""
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4 or not fields[0].endswith(".service"):
            continue
        unit, load, _active, sub = fields[:4]
        if load == "not-found":
            continue    # referenced by another unit but not installed
        rows.append({"unit": unit, "state": sub, "description": fields[4] if len(fields) > 4 else ""})
    rows.sort(key=lambda r: (_SERVICE_ORDER.get(r["state"], 3), r["unit"]))
    return rows


def parse_listening(text: str) -> list[dict]:
    """`ss -H -tulpn`: what is listening, and which program when we may know."""
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        address, _, port = fields[4].rpartition(":")
        program = re.search(r'\(\("([^"]+)"', line)
        rows.append({
            "protocol": fields[0], "address": address, "port": port,
            # Only root is told which program owns another user's socket.
            "program": program.group(1) if program else "",
        })
    rows.sort(key=lambda r: (r["protocol"], int(r["port"]) if r["port"].isdigit() else 0, r["address"]))
    return rows


def parse_df(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split(None, 6)
        if len(fields) == 7:
            keys = ("filesystem", "type", "size", "used", "free", "use", "mount")
            rows.append(dict(zip(keys, fields)))
    return rows


def parse_ip_addresses(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            rows.append({"name": fields[0], "state": fields[1].capitalize(),
                         "addresses": ", ".join(fields[2:])})
    return rows


def parse_ip_routes(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue

        def after(word):
            return fields[fields.index(word) + 1] if word in fields[:-1] else ""

        rows.append({"destination": fields[0], "gateway": after("via"), "interface": after("dev"),
                     "source": after("src"), "metric": after("metric")})
    return rows


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split(None, 4)
        # `ps` always catches itself mid-run at the top of its own list.
        if len(fields) == 5 and fields[4] != "ps":
            rows.append(dict(zip(("pid", "user", "cpu", "memory", "command"), fields)))
    return rows


class LinuxPanel(Panel):
    platform = "linux"
    sections = [
        {"id": "overview", "label": "Overview"},
        {"id": "services", "label": "Services"},
        {"id": "listening", "label": "Listening ports"},
        {"id": "disks", "label": "Disks"},
        {"id": "addresses", "label": "Addresses"},
        {"id": "routes", "label": "Routes"},
        {"id": "processes", "label": "Processes"},
    ]

    def __init__(self, session: Session):
        super().__init__(session)
        # Root needs no sudo; anyone else gets `sudo -n`, which fails at once
        # instead of waiting for a password nobody is there to type.
        self._sudo = "" if session.run("id -u").strip() == "0" else "sudo -n "

    @staticmethod
    def _rows(records: list[dict], columns: list[dict], key) -> dict:
        return {"columns": columns,
                "rows": [{"id": key(r), "cells": r} for r in records], "actions": []}

    def read_overview(self) -> dict:
        parts = [p.strip() for p in self.session.run(_LINUX_OVERVIEW).split(_SEP)]
        parts += [""] * (8 - len(parts))
        name, system, kernel, uptime, load, cores, memory, user = parts[:8]
        mem = {k: int(v.split()[0]) for k, v in
               (line.split(":", 1) for line in memory.splitlines() if ":" in line)
               if v.split() and v.split()[0].isdigit()}
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        seconds = uptime.split()[0] if uptime.split() else ""
        shown = [
            ("Name", name),
            ("System", system),
            ("Kernel", kernel),
            ("Uptime", _days(float(seconds)) if seconds.replace(".", "", 1).isdigit() else ""),
            ("Load (1, 5, 15 min)", " ".join(load.split()[:3])),
            ("CPU cores", cores),
            ("Memory free", f"{available / 1048576:.1f} GiB of {total / 1048576:.1f} GiB"
                            if total and available is not None else ""),
            ("Signed in as", user + (" (changes run directly)" if not self._sudo
                                     else " (changes run through sudo)")),
        ]
        return {
            "columns": [_column("item", "Item"), _column("value", "Value")],
            "rows": [{"id": label, "cells": {"item": label, "value": value or "—"}} for label, value in shown],
            "actions": [],
        }

    def read_services(self) -> dict:
        records = parse_services(self.session.run(
            "systemctl list-units --type=service --all --plain --no-legend --no-pager"))
        table = self._rows(records, [_column("unit", "Service", True), _column("state", "State"),
                                     _column("description", "Description")], lambda r: r["unit"])
        table["actions"] = [
            {"id": "restart", "label": "Restart", "scope": "row", "fields": []},
            {"id": "stop", "label": "Stop", "scope": "row", "fields": [], "when": {"state": "running"}},
            {"id": "start", "label": "Start", "scope": "row", "fields": [], "unless": {"state": "running"}},
        ]
        return table

    def read_listening(self) -> dict:
        return self._rows(parse_listening(self.session.run("ss -H -tulpn 2>/dev/null")), [
            _column("protocol", "Protocol"), _column("port", "Port", True),
            _column("address", "On address", True), _column("program", "Program")],
            lambda r: f"{r['protocol']}/{r['address']}:{r['port']}")

    def read_disks(self) -> dict:
        records = parse_df(self.session.run(
            "df -PTh -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null"))
        return self._rows(records, [
            _column("mount", "Mounted at", True), _column("use", "Used"), _column("free", "Free"),
            _column("size", "Size"), _column("type", "Type"), _column("filesystem", "Device", True)],
            lambda r: r["mount"])

    def read_addresses(self) -> dict:
        return self._rows(parse_ip_addresses(self.session.run("ip -br address")), [
            _column("name", "Interface", True), _column("state", "State"),
            _column("addresses", "Addresses", True)], lambda r: r["name"])

    def read_routes(self) -> dict:
        records = parse_ip_routes(self.session.run("ip route"))
        return self._rows(records, [
            _column("destination", "Destination", True), _column("gateway", "Gateway", True),
            _column("interface", "Interface", True), _column("source", "Source", True),
            _column("metric", "Metric")], lambda r: f"{r['destination']} {r['interface']} {r['metric']}")

    def read_processes(self) -> dict:
        records = parse_ps(self.session.run("ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -n 26"))
        return self._rows(records, [
            _column("command", "Program"), _column("cpu", "CPU %"), _column("memory", "Memory %"),
            _column("user", "User"), _column("pid", "PID", True)], lambda r: r["pid"])

    # -- changes
    def _service(self, row, verb: str, warning: str = "") -> dict:
        unit = self._row("services", row)["unit"]
        if not re.fullmatch(r"[A-Za-z0-9@._:-]+\.service", unit):
            raise PanelError(f"Unexpected service name: {unit!r}")
        if unit.startswith(("ssh.", "sshd.")) and verb != "start":
            warning = ("This is the SSH service you are connected through. Restarting it normally "
                       "keeps existing sessions; stopping it locks you out of the server.")
        return {"title": f"{verb.capitalize()} {unit}",
                "commands": [f"{self._sudo}systemctl {verb} {unit}"], "warning": warning}

    def plan_services_restart(self, row, values) -> dict:
        return self._service(row, "restart", "Whatever the service is doing is interrupted while it restarts.")

    def plan_services_stop(self, row, values) -> dict:
        return self._service(row, "stop", "The service stays stopped until it is started again or the server restarts.")

    def plan_services_start(self, row, values) -> dict:
        return self._service(row, "start")

    def run_changes(self, commands: list[str], tolerate=()) -> dict:
        outputs = []
        for command in commands:
            # The exit status says whether it worked; systemctl is silent on success.
            text = self.session.run(f"sh -c '{command} 2>&1; echo {_SEP}$?'")
            output, _, status = text.rpartition(_SEP)
            output = output.strip()
            if status.strip() != "0":
                if "password is required" in output:
                    output += ("\nThis account cannot use sudo without typing a password. Sign in "
                               "as root, or allow it passwordless sudo for systemctl.")
                return {"ok": False, "output": f"{command}\n{output}".strip()}
            if output:
                outputs.append(output)
        return {"ok": True, "output": "\n".join(outputs)}


# --- Windows ----------------------------------------------------------------
def _ps_table(session, script: str) -> list[dict]:
    wrapped = f"ConvertTo-Json -Compress -Depth 3 -InputObject @({script})"
    return powershell.rows(session.run(powershell.command(wrapped)))


def _gib(value) -> str:
    return f"{float(value) / 2 ** 30:.1f} GiB" if isinstance(value, (int, float)) else ""


class WindowsPanel(Panel):
    """Windows, through its OpenSSH server.  Everything is asked in PowerShell
    and answered as JSON, so there is no column-guessing as with the others."""

    platform = "windows"
    sections = [
        {"id": "overview", "label": "Overview"},
        {"id": "services", "label": "Services"},
        {"id": "listening", "label": "Listening ports"},
        {"id": "disks", "label": "Disks"},
        {"id": "addresses", "label": "Addresses"},
        {"id": "processes", "label": "Processes"},
        {"id": "events", "label": "Recent errors"},
    ]

    def _table(self, records, columns, key, convert=None) -> dict:
        rows = []
        for record in records:
            cells = convert(record) if convert else {c["key"]: record.get(c["key"]) for c in columns}
            rows.append({"id": str(key(record)),
                         "cells": {k: "" if v is None else str(v) for k, v in cells.items()}})
        return {"columns": columns, "rows": rows, "actions": []}

    def read_overview(self) -> dict:
        record = (_ps_table(self.session, (
            "$o=Get-CimInstance Win32_OperatingSystem;$c=Get-CimInstance Win32_ComputerSystem;"
            "$p=Get-CimInstance Win32_Processor|Select-Object -First 1;"
            "@{name=$c.Name;domain=$c.Domain;system=$o.Caption;version=$o.Version;"
            "maker=$c.Manufacturer;model=$c.Model;cpu=$p.Name;cores=[int]$c.NumberOfLogicalProcessors;"
            "memory=[double]$c.TotalPhysicalMemory;free=[double]$o.FreePhysicalMemory*1024;"
            "uptime=[int]((Get-Date)-$o.LastBootUpTime).TotalSeconds;user=$env:USERNAME}")) or [{}])[0]
        uptime = record.get("uptime")
        shown = [
            ("Name", record.get("name")), ("Domain or workgroup", record.get("domain")),
            ("System", record.get("system")), ("Version", record.get("version")),
            ("Hardware", " ".join(str(record.get(k) or "") for k in ("maker", "model")).strip()),
            ("Processor", f"{str(record.get('cpu') or '').strip()}, {record.get('cores') or '?'} threads"),
            ("Memory free", f"{_gib(record.get('free'))} of {_gib(record.get('memory'))}"),
            ("Uptime", _days(uptime) if isinstance(uptime, (int, float)) else ""),
            ("Signed in as", record.get("user")),
        ]
        return {
            "columns": [_column("item", "Item"), _column("value", "Value")],
            "rows": [{"id": label, "cells": {"item": label, "value": str(value or "—")}} for label, value in shown],
            "actions": [],
        }

    def read_services(self) -> dict:
        records = _ps_table(self.session, (
            "Get-Service|ForEach-Object{@{name=$_.Name;title=$_.DisplayName;"
            "state=[string]$_.Status;start=[string]$_.StartType}}"))
        # What should be running but is not comes first.
        records.sort(key=lambda r: (not (r.get("start") == "Automatic" and r.get("state") != "Running"),
                                    r.get("state") != "Running", str(r.get("name")).lower()))
        table = self._table(records, [_column("name", "Service", True), _column("state", "State"),
                                      _column("start", "Starts"), _column("title", "Description")],
                            lambda r: r.get("name"))
        table["actions"] = [
            {"id": "restart", "label": "Restart", "scope": "row", "fields": [], "when": {"state": "Running"}},
            {"id": "stop", "label": "Stop", "scope": "row", "fields": [], "when": {"state": "Running"}},
            {"id": "start", "label": "Start", "scope": "row", "fields": [], "unless": {"state": "Running"}},
        ]
        return table

    def read_listening(self) -> dict:
        records = _ps_table(self.session, (
            "Get-NetTCPConnection -State Listen|ForEach-Object{@{address=$_.LocalAddress;"
            "port=[int]$_.LocalPort;program=(Get-Process -Id $_.OwningProcess -ErrorAction "
            "SilentlyContinue).ProcessName}}"))
        records.sort(key=lambda r: (r.get("port") or 0, str(r.get("address"))))
        return self._table(records, [_column("port", "Port", True), _column("address", "On address", True),
                                     _column("program", "Program")],
                           lambda r: f"{r.get('address')}:{r.get('port')}")

    def read_disks(self) -> dict:
        records = _ps_table(self.session, (
            "Get-Volume|Where-Object DriveLetter|ForEach-Object{@{drive=[string]$_.DriveLetter;"
            "label=$_.FileSystemLabel;type=$_.FileSystem;size=[double]$_.Size;"
            "free=[double]$_.SizeRemaining}}"))
        records.sort(key=lambda r: str(r.get("drive")))

        def convert(r):
            size, free = r.get("size") or 0, r.get("free") or 0
            return {"drive": f"{r.get('drive')}:", "label": r.get("label"),
                    "use": f"{100 * (size - free) / size:.0f}%" if size else "",
                    "free": _gib(free), "size": _gib(size), "type": r.get("type")}

        return self._table(records, [_column("drive", "Drive", True), _column("label", "Label"),
                                     _column("use", "Used"), _column("free", "Free"),
                                     _column("size", "Size"), _column("type", "Type")],
                           lambda r: r.get("drive"), convert)

    def read_addresses(self) -> dict:
        records = _ps_table(self.session, (
            "Get-NetIPAddress -AddressFamily IPv4|ForEach-Object{@{name=$_.InterfaceAlias;"
            "address=\"$($_.IPAddress)/$($_.PrefixLength)\";origin=[string]$_.PrefixOrigin}}"))
        return self._table(records, [_column("name", "Interface"), _column("address", "Address", True),
                                     _column("origin", "Set by")],
                           lambda r: f"{r.get('name')} {r.get('address')}")

    def read_processes(self) -> dict:
        records = _ps_table(self.session, (
            "Get-Process|Sort-Object CPU -Descending|Select-Object -First 25|ForEach-Object{"
            "@{name=$_.ProcessName;pid=$_.Id;cpu=[int]$_.CPU;memory=[int]($_.WorkingSet64/1MB)}}"))
        records.sort(key=lambda r: -(r.get("cpu") or 0))
        return self._table(records, [_column("name", "Program"), _column("cpu", "CPU seconds"),
                                     _column("memory", "Memory MiB"), _column("pid", "PID", True)],
                           lambda r: r.get("pid"))

    def read_events(self) -> dict:
        records = _ps_table(self.session, (
            "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2} -MaxEvents 40 "
            "-ErrorAction SilentlyContinue|ForEach-Object{@{time=$_.TimeCreated.ToString("
            "'yyyy-MM-dd HH:mm');source=$_.ProviderName;id=$_.Id;"
            "message=([string]$_.Message -split \"`n\")[0]}}"))
        return self._table(records, [_column("time", "When", True), _column("source", "Source"),
                                     _column("id", "Event"), _column("message", "Message")],
                           lambda r: f"{r.get('time')} {r.get('id')} {r.get('source')}")

    # -- changes
    def _service(self, row, cmdlet: str, verb: str, warning: str = "") -> dict:
        name = self._row("services", row)["name"]
        if name.lower() == "sshd" and cmdlet != "Start-Service":
            warning = ("This is the SSH service you are connected through. Stopping it locks "
                       "you out of the server until someone starts it locally.")
        return {"title": f"{verb} {name}",
                "commands": [f"{cmdlet} -Name {powershell.quote(name)}"], "warning": warning}

    def plan_services_restart(self, row, values) -> dict:
        return self._service(row, "Restart-Service", "Restart",
                             "Whatever the service is doing is interrupted while it restarts.")

    def plan_services_stop(self, row, values) -> dict:
        return self._service(row, "Stop-Service", "Stop",
                             "The service stays stopped until it is started again or the server restarts.")

    def plan_services_start(self, row, values) -> dict:
        return self._service(row, "Start-Service", "Start")

    def run_changes(self, commands: list[str], tolerate=()) -> dict:
        for command in commands:
            # The cmdlets say nothing on success; turn a failure into text we can see.
            script = (f"try{{{command} -ErrorAction Stop;'nettool-ok'}}"
                      "catch{'nettool-failed: '+$_.Exception.Message}")
            output = self.session.run(powershell.command(script)).strip()
            if "nettool-ok" not in output:
                reason = output.replace("nettool-failed: ", "") or "The server gave no answer."
                return {"ok": False, "output": f"{command}\n{reason}"}
        return {"ok": True, "output": ""}


def _is_ios_error(line: str) -> bool:
    """IOS marks a rejected command with a leading "%" ("% Invalid input").

    Log messages that happen to arrive mid-command start the same way but
    carry a facility code ("%SYS-5-CONFIG_I:"), and are not errors.
    """
    line = line.strip()
    if line.startswith("Command rejected"):
        return True
    return line.startswith("%") and not re.match(r"^%[A-Z0-9_]+-\d-[A-Z0-9_]+:", line)


def open_panel(device: dict, credentials: dict, session_factory=Session) -> Panel:
    """Sign in to a device and return the panel for its platform."""
    kind = PANELS.get(device.get("platform"))
    if kind is None:
        raise PanelError("There is no management panel for this kind of device yet.")
    if not device.get("address"):
        raise PanelError("This entry has no address.")
    fields = device.get("fields") or {}
    port = (credentials.get("mgmt_port") or credentials.get("port")
            or fields.get("mgmt_port") or fields.get("port"))
    session = session_factory(
        device["address"], int(port) if str(port or "").isdigit() else 22,
        credentials.get("username") or "",
        password=credentials.get("password"),
        private_key=credentials.get("private_key"),
        passphrase=credentials.get("passphrase"),
    )
    try:
        if kind is CiscoPanel:
            return CiscoPanel(session, credentials.get("enable_password"))
        return kind(session)
    except Exception:
        session.close()
        raise


PANELS = {"mikrotik": MikrotikPanel, "cisco_ios": CiscoPanel, "linux": LinuxPanel,
          "windows": WindowsPanel}
