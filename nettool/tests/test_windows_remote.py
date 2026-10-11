"""Tests for Windows over SSH (PowerShell + JSON) and for remote desktop.

Neither a Windows server nor an RDP client is available where these run, so
they pin down what is sent and how answers are read.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nettool_engine import collectors, powershell, remote
from nettool_engine.panels import PanelError, WindowsPanel


class TestPowershell(unittest.TestCase):
    def test_command_round_trips_and_needs_no_quoting(self):
        script = """Get-Service | Where-Object { $_.Name -eq 'W32Time' } ; "double" & | < > ^ %"""
        line = powershell.command(script)
        self.assertEqual(powershell.decode(line), script)
        self.assertRegex(line, r"^powershell -NoProfile -NonInteractive -EncodedCommand [A-Za-z0-9+/=]+$")

    def test_quote(self):
        self.assertEqual(powershell.quote("Spooler"), "'Spooler'")
        self.assertEqual(powershell.quote("it's; Stop-Computer"), "'it''s; Stop-Computer'")

    def test_json_inside_a_banner(self):
        self.assertEqual(powershell.rows('WARNING: something\r\n[{"a":1},{"a":2}]\r\n'), [{"a": 1}, {"a": 2}])
        self.assertEqual(powershell.rows('{"a":1}'), [{"a": 1}])      # one object, not wrapped
        self.assertEqual(powershell.rows("Access is denied."), [])
        self.assertEqual(powershell.rows('[{"a":1}, "stray"]'), [{"a": 1}])


class TestWindowsCollector(unittest.TestCase):
    def reading(self, rx, tx):
        return [json.dumps({
            "cpu": 23, "total": 16777216, "free": 4194304, "uptime": 86400,
            "adapters": [
                {"name": "Ethernet", "status": "Up", "virtual": False, "rx": rx, "tx": tx},
                {"name": "vEthernet (Default Switch)", "status": "Up", "virtual": True, "rx": 9e9, "tx": 9e9},
                {"name": "Wi-Fi", "status": "Disconnected", "virtual": False, "rx": 0, "tx": 0},
            ],
        })]

    def test_reading_and_rates(self):
        first = collectors.parse_windows(self.reading(1000, 2000))
        second = collectors.parse_windows(self.reading(1000 + 125000, 2000 + 25000))
        done = collectors.finish(first, second, 10.0)
        self.assertEqual(done["cpu"], 23.0)
        self.assertEqual(done["mem_total"], 16 * 2 ** 30)
        self.assertEqual(done["mem_used"], 12 * 2 ** 30)
        self.assertEqual(done["uptime"], 86400.0)
        # The virtual switch is listed but not added into the total.
        self.assertEqual((done["rx_bps"], done["tx_bps"]), (100000.0, 20000.0))
        states = {i["name"]: i["up"] for i in done["ifaces"]}
        self.assertEqual(states, {"Ethernet": True, "vEthernet (Default Switch)": True, "Wi-Fi": False})

    def test_an_error_instead_of_json(self):
        raw = collectors.parse_windows(["'powershell' is not recognized as an internal or external command"])
        self.assertIsNone(raw["cpu"])
        self.assertEqual(raw["ifaces"], [])

    def test_it_is_registered_and_asked_less_often(self):
        collector = collectors.COLLECTORS["windows"]
        self.assertEqual(collector.every, 3)
        self.assertIn("Win32_OperatingSystem", powershell.decode(collector.commands[0]))


class Answers:
    """A session whose answer depends on what the decoded script mentions."""

    def __init__(self, table):
        self.table = table
        self.scripts = []
        self.prompt = ""

    def run(self, command_line):
        script = powershell.decode(command_line)
        self.scripts.append(script)
        for key, value in self.table.items():
            if key in script:
                return value if isinstance(value, str) else json.dumps(value)
        return "[]"

    def close(self):
        pass


SERVICES = [
    {"name": "Spooler", "title": "Print Spooler", "state": "Running", "start": "Automatic"},
    {"name": "W32Time", "title": "Windows Time", "state": "Stopped", "start": "Automatic"},
    {"name": "Fax", "title": "Fax", "state": "Stopped", "start": "Manual"},
    {"name": "sshd", "title": "OpenSSH SSH Server", "state": "Running", "start": "Automatic"},
]


class TestWindowsPanel(unittest.TestCase):
    def panel(self, **extra):
        return WindowsPanel(Answers({"Get-Service": SERVICES, **extra}))

    def test_services_put_what_should_be_running_first(self):
        table = self.panel().read("services")
        self.assertEqual([r["id"] for r in table["rows"]], ["W32Time", "Spooler", "sshd", "Fax"])
        self.assertEqual(table["rows"][0]["cells"]["title"], "Windows Time")

    def test_disks(self):
        table = self.panel(**{"Get-Volume": [
            {"drive": "C", "label": "System", "type": "NTFS", "size": 100 * 2 ** 30, "free": 25 * 2 ** 30},
        ]}).read("disks")
        self.assertEqual(table["rows"][0]["cells"],
                         {"drive": "C:", "label": "System", "use": "75%", "free": "25.0 GiB",
                          "size": "100.0 GiB", "type": "NTFS"})

    def test_overview_and_empty_answers(self):
        panel = self.panel(**{"Win32_ComputerSystem": [{
            "name": "DC01", "domain": "office.local", "system": "Microsoft Windows Server 2022 Standard",
            "version": "10.0.20348", "maker": "Dell Inc.", "model": "PowerEdge R350",
            "cpu": "Intel(R) Xeon(R) E-2334 ", "cores": 8, "memory": 32 * 2 ** 30,
            "free": 20 * 2 ** 30, "uptime": 200000, "user": "Administrator"}]})
        rows = {r["cells"]["item"]: r["cells"]["value"] for r in panel.read("overview")["rows"]}
        self.assertEqual(rows["Hardware"], "Dell Inc. PowerEdge R350")
        self.assertEqual(rows["Memory free"], "20.0 GiB of 32.0 GiB")
        self.assertEqual(rows["Uptime"], "2 days, 7 hours, 33 minutes")
        self.assertEqual(panel.read("events")["rows"], [])          # no errors logged is not a failure

    def test_service_changes(self):
        panel = self.panel()
        panel.read("services")
        self.assertEqual(panel.plan("services", "start", "W32Time", {})["commands"],
                         ["Start-Service -Name 'W32Time'"])
        self.assertIn("locks you out", panel.plan("services", "stop", "sshd", {})["warning"])
        with self.assertRaises(PanelError):
            panel.plan("services", "stop", "Spooler'; Stop-Computer; '", {})

    def test_apply_reads_success_and_failure(self):
        ok = self.panel(**{"Restart-Service": "nettool-ok\r\n"})
        ok.read("services")
        self.assertEqual(ok.apply("services", "restart", "Spooler", {}), {"ok": True, "output": ""})
        self.assertIn("Restart-Service -Name 'Spooler' -ErrorAction Stop", ok.session.scripts[-1])

        bad = self.panel(**{"Stop-Service": "nettool-failed: Cannot stop service 'Spooler' because it has dependent services.\r\n"})
        bad.read("services")
        result = bad.apply("services", "stop", "Spooler", {})
        self.assertFalse(result["ok"])
        self.assertIn("dependent services", result["output"])


class FakeProcess:
    def __init__(self):
        self.stdin = mock.Mock()


class TestRemote(unittest.TestCase):
    def device(self, **fields):
        platform = fields.pop("platform", "windows")
        address = fields.pop("address", "10.0.0.50")
        return {"id": "d", "platform": platform, "address": address, "fields": fields}

    def test_what_is_available(self):
        self.assertEqual(remote.available(self.device()), ["rdp"])
        self.assertEqual(remote.available(self.device(rustdesk_id="123 456 789")), ["rdp", "rustdesk"])
        self.assertEqual(remote.available(self.device(platform="linux")), [])
        # A laptop with no fixed address: RustDesk only.
        self.assertEqual(remote.available(self.device(address="", rustdesk_id="123456789")), ["rustdesk"])

    def test_rustdesk_id_is_cleaned_and_checked(self):
        self.assertEqual(remote.rustdesk_id(self.device(rustdesk_id="123 456-789")), "123456789")
        self.assertEqual(remote.rustdesk_id(self.device(rustdesk_id="pc01@relay.example.com")), "pc01@relay.example.com")
        for bad in ("--password x", "-h", "1; rm -rf ~", "", "12"):
            self.assertEqual(remote.rustdesk_id(self.device(rustdesk_id=bad)), "", bad)

    def test_rdp_arguments_never_hold_the_password(self):
        calls = []

        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return FakeProcess()

        with mock.patch.object(remote, "IS_WINDOWS", False), \
             mock.patch.object(remote, "_find", lambda names: "/usr/bin/xfreerdp3"):
            result = remote.open_rdp(self.device(), {"username": "OFFICE\\admin", "password": "s3cret"}, popen)
        command, kwargs = calls[0]
        self.assertEqual(command, ["/usr/bin/xfreerdp3", "/v:10.0.0.50", "/cert:tofu", "/dynamic-resolution",
                                   "+clipboard", "/from-stdin", "/u:admin", "/d:OFFICE"])
        self.assertNotIn("s3cret", " ".join(command))
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertTrue(result["signed_in"])

    def test_older_freerdp_spells_the_certificate_option_differently(self):
        self.assertIn("/cert-tofu", remote.rdp_command("/usr/bin/xfreerdp", "h", "u"))
        self.assertIn("/cert:tofu", remote.rdp_command("/usr/bin/sdl-freerdp3", "h", "u"))

    def test_missing_programs_say_how_to_get_them(self):
        with mock.patch.object(remote, "IS_WINDOWS", False), mock.patch.object(remote, "_find", lambda names: None):
            with self.assertRaises(remote.RemoteError) as rdp:
                remote.open_rdp(self.device(), {"username": "a", "password": "b"})
            with self.assertRaises(remote.RemoteError) as rust:
                remote.open_rustdesk(self.device(rustdesk_id="123456789"))
        self.assertIn("freerdp", str(rdp.exception))
        self.assertIn("rustdesk.com", str(rust.exception))

    def test_rustdesk_gets_the_id_and_nothing_secret(self):
        calls = []
        with mock.patch.object(remote, "_find", lambda names: "/usr/bin/rustdesk"):
            remote.open_rustdesk(self.device(rustdesk_id="123 456 789"),
                                 lambda command, **kw: calls.append(command))
        self.assertEqual(calls, [["/usr/bin/rustdesk", "--connect", "123456789"]])

    def test_bad_addresses_are_refused(self):
        for address in ("", "-v", "host /p:x", "a;b"):
            with self.assertRaises(remote.RemoteError):
                remote.open_rdp(self.device(address=address), {})


if __name__ == "__main__":
    unittest.main()
