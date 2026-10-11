"""Tests for the management panels.

Reading is tested against captured device output.  Changes are tested for the
exact commands they produce and for what they refuse -- this is the code that
types into a device's configuration prompt.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nettool_engine import panels
from nettool_engine.panels import CiscoPanel, LinuxPanel, MikrotikPanel, PanelError, open_panel


class Scripted:
    """A session that answers from a table and records what it was sent."""

    def __init__(self, answers=None, prompt="SW#", enable_to="SW#"):
        self.answers = answers or {}
        self.sent = []
        self.prompt = prompt
        self._enable_to = enable_to
        self.enabled_with = None
        self.closed = False

    def _answer(self, command):
        self.sent.append(command)
        for key, value in self.answers.items():
            if key in command:
                return value
        return ""

    run = run_in_shell = _answer

    def enable(self, secret):
        if self.prompt.endswith("#"):
            return True
        self.enabled_with = secret
        if secret:
            self.prompt = self._enable_to
        return self.prompt.endswith("#")

    def close(self):
        self.closed = True


ROS_INTERFACES = (
    ".id=*1;name=ether1;type=ether;mtu=1500;mac-address=4C:5E:0C:12:34:56;running=true;disabled=false;comment=uplink; to ISP\r\n"
    ".id=*2;name=ether2;type=ether;mtu=1500;mac-address=4C:5E:0C:12:34:57;running=false;disabled=false\r\n"
    ".id=*3;name=guest wifi;type=wlan;mtu=1500;running=false;disabled=true;comment=a=b\r\n"
)


class TestMikrotik(unittest.TestCase):
    def panel(self, **answers):
        answers.setdefault("/interface print", ROS_INTERFACES)
        return MikrotikPanel(Scripted(answers))

    def test_as_value_keeps_semicolons_and_equals_inside_values(self):
        records = panels.parse_as_value(ROS_INTERFACES)
        self.assertEqual(records[0]["comment"], "uplink; to ISP")
        self.assertEqual(records[2]["comment"], "a=b")
        self.assertEqual(records[2]["name"], "guest wifi")

    def test_interfaces_table(self):
        table = self.panel().read("interfaces")
        states = {row["id"]: row["cells"]["state"] for row in table["rows"]}
        self.assertEqual(states, {"ether1": "Running", "ether2": "Not running", "guest wifi": "Disabled"})
        self.assertEqual(table["rows"][1]["cells"]["comment"], "")     # absent, not an error

    def test_overview(self):
        answer = ("identity=R-CORE;version=7.15.3 (stable);board-name=CCR2004;architecture-name=arm64;"
                  "cpu-count=4;cpu-load=7;free-memory=3221225472;total-memory=4294967296;"
                  "free-hdd-space=104857600;total-hdd-space=134217728;uptime=1w2d03:04:05\n")
        rows = dict((r["cells"]["item"], r["cells"]["value"])
                    for r in self.panel(**{"system identity": answer}).read("overview")["rows"])
        self.assertEqual(rows["Name"], "R-CORE")
        self.assertEqual(rows["Memory free"], "3072 MiB of 4096 MiB")
        self.assertEqual(rows["Uptime"], "9 days, 3 hours, 4 minutes")

    def test_change_commands(self):
        panel = self.panel()
        panel.read("interfaces")
        self.assertEqual(panel.plan("interfaces", "disable", "ether2", {})["commands"],
                         ['/interface disable [find name="ether2"]'])
        self.assertTrue(panel.plan("interfaces", "disable", "ether2", {})["warning"])
        self.assertEqual(panel.plan("interfaces", "comment", "guest wifi", {"comment": "lobby"})["commands"],
                         ['/interface set [find name="guest wifi"] comment="lobby"'])

    def test_values_cannot_break_out_of_their_quotes(self):
        panel = self.panel()
        panel.read("interfaces")
        hostile = 'x" ; /user add name=evil group=full password=$pw ; :put "'
        command = panel.plan("interfaces", "comment", "ether1", {"comment": hostile})["commands"][0]
        self.assertEqual(
            command,
            '/interface set [find name="ether1"] comment='
            '"x\\" ; /user add name=evil group=full password=\\$pw ; :put \\""',
        )
        for bad in ("two\nlines", "tab\tbed", "x" * 201):
            with self.assertRaises(PanelError):
                panel.plan("interfaces", "comment", "ether1", {"comment": bad})

    def test_only_rows_the_device_listed_can_be_acted_on(self):
        panel = self.panel()
        with self.assertRaises(PanelError):                 # nothing read yet
            panel.plan("interfaces", "disable", "ether1", {})
        panel.read("interfaces")
        with self.assertRaises(PanelError):
            panel.plan("interfaces", "disable", 'x"] ; /system reset-configuration ; [find name="', {})
        with self.assertRaises(PanelError):
            panel.plan("interfaces", "reboot", "ether1", {})
        with self.assertRaises(PanelError):
            panel.read("__init__")

    def test_apply_runs_exactly_the_planned_commands(self):
        panel = self.panel()
        panel.read("interfaces")
        panel.session.sent.clear()
        result = panel.apply("interfaces", "enable", "guest wifi", {})
        self.assertEqual(panel.session.sent, ['/interface enable [find name="guest wifi"]'])
        self.assertEqual(result, {"ok": True, "output": ""})

    def test_a_refusal_from_the_router_is_reported(self):
        panel = self.panel(**{"/interface disable": "failure: not permitted\n"})
        panel.read("interfaces")
        self.assertEqual(panel.apply("interfaces", "disable", "ether1", {}),
                         {"ok": False, "output": "failure: not permitted"})


STATUS = """
Port      Name               Status       Vlan       Duplex  Speed Type
Gi0/1     Uplink to core     connected    trunk      a-full a-1000 10/100/1000BaseTX
Gi0/2                        notconnect   10           auto   auto 10/100/1000BaseTX
Gi0/3     Reception printer  disabled     20           auto   auto 10/100/1000BaseTX
Te1/0/1                      connected    1            full    10G SFP-10GBase-SR
"""

VLANS = """
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/4, Gi0/5, Gi0/6, Gi0/7
                                                Gi0/8, Gi0/9
10   STAFF                            active    Gi0/2
1002 fddi-default                     act/unsup 
"""

VERSION = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E3, RELEASE SOFTWARE (fc3)
SW-FLOOR1 uptime is 3 weeks, 2 days, 4 hours, 15 minutes
System returned to ROM by power-on
System image file is "flash:/c2960x-universalk9-mz.152-7.E3.bin"
cisco WS-C2960X-48TS-L (APM86XXX) processor (revision A0) with 524288K bytes of memory.
Processor board ID FOC1234ABCD
"""


class TestCiscoReading(unittest.TestCase):
    def test_ports_by_column_position(self):
        rows = {r["port"]: r for r in panels.parse_interfaces_status(STATUS)}
        self.assertEqual(rows["Gi0/1"]["name"], "Uplink to core")
        self.assertEqual((rows["Gi0/1"]["status"], rows["Gi0/1"]["vlan"]), ("connected", "trunk"))
        self.assertEqual(rows["Gi0/2"]["name"], "")              # empty, columns still line up
        self.assertEqual((rows["Gi0/2"]["vlan"], rows["Gi0/2"]["duplex"]), ("10", "auto"))
        self.assertEqual(rows["Te1/0/1"]["speed"], "10G")
        self.assertEqual(rows["Gi0/1"]["type"], "10/100/1000BaseTX")

    def test_vlans_with_wrapped_port_lists(self):
        rows = {r["vlan"]: r for r in panels.parse_vlan_brief(VLANS)}
        self.assertEqual(rows["1"]["ports"], "Gi0/4, Gi0/5, Gi0/6, Gi0/7 Gi0/8, Gi0/9")
        self.assertEqual(rows["10"]["name"], "STAFF")
        self.assertEqual(rows["1002"]["ports"], "")

    def test_ip_brief_with_multi_word_status(self):
        rows = panels.parse_ip_brief(
            "Interface              IP-Address      OK? Method Status                Protocol\n"
            "Vlan99                 10.0.99.2       YES NVRAM  up                    up\n"
            "GigabitEthernet0/1     unassigned      YES unset  administratively down down\n")
        self.assertEqual(rows[1]["status"], "administratively down")
        self.assertEqual(rows[0]["address"], "10.0.99.2")

    def test_mac_table(self):
        rows = panels.parse_mac_table(
            "          Mac Address Table\n-----\nVlan    Mac Address       Type        Ports\n"
            "  10    0011.2233.4455    DYNAMIC     Gi0/2\n All    0100.0ccc.cccc    STATIC      CPU\n")
        self.assertEqual(rows[0], {"vlan": "10", "mac": "0011.2233.4455", "type": "Dynamic", "port": "Gi0/2"})
        self.assertEqual(len(rows), 2)

    def test_version(self):
        info = dict(panels.parse_version(VERSION))
        self.assertEqual(info["Name"], "SW-FLOOR1")
        self.assertEqual(info["Model"], "WS-C2960X-48TS-L")
        self.assertEqual(info["Software"], "15.2(7)E3")
        self.assertEqual(info["Serial number"], "FOC1234ABCD")
        self.assertEqual(info["Last restart"], "power-on")


class TestCiscoChanges(unittest.TestCase):
    def panel(self, **answers):
        answers.setdefault("show interfaces status", STATUS)
        panel = CiscoPanel(Scripted(answers))
        panel.read("ports")
        panel.session.sent.clear()
        return panel

    def test_description(self):
        plan = self.panel().plan("ports", "describe", "Gi0/2", {"description": "Meeting room AP"})
        self.assertEqual(plan["commands"], ["configure terminal", "interface Gi0/2",
                                            "description Meeting room AP", "end"])
        cleared = self.panel().plan("ports", "describe", "Gi0/2", {"description": ""})
        self.assertIn("no description", cleared["commands"])

    def test_access_vlan_and_the_trunk_warning(self):
        panel = self.panel()
        plan = panel.plan("ports", "vlan", "Gi0/2", {"vlan": "30"})
        self.assertEqual(plan["commands"], ["configure terminal", "interface Gi0/2",
                                            "switchport mode access", "switchport access vlan 30", "end"])
        self.assertNotIn("trunk", plan["warning"])
        self.assertIn("trunk", panel.plan("ports", "vlan", "Gi0/1", {"vlan": 30})["warning"])

    def test_bad_values_are_refused(self):
        panel = self.panel()
        for vlan in ("0", "4095", "ten", "10\nshutdown", ""):
            with self.assertRaises(PanelError):
                panel.plan("ports", "vlan", "Gi0/2", {"vlan": vlan})
        for text in ("what?", "line\nno shutdown", "x" * 201):
            with self.assertRaises(PanelError):
                panel.plan("ports", "describe", "Gi0/2", {"description": text})
        for name in ("has space", "semi;colon", "x" * 33):
            with self.assertRaises(PanelError):
                panel.plan("vlans", "create", None, {"vlan": 30, "name": name})
        with self.assertRaises(PanelError):
            panel.plan("ports", "shutdown", "Gi0/99", {})          # not a port it listed

    def test_create_vlan_and_save(self):
        panel = self.panel()
        self.assertEqual(panel.plan("vlans", "create", None, {"vlan": 30, "name": "GUEST"})["commands"],
                         ["configure terminal", "vlan 30", "name GUEST", "end"])
        self.assertEqual(panel.plan("overview", "save", None, {})["commands"], ["write memory"])

    def test_apply_stops_at_a_rejection_and_leaves_config_mode(self):
        panel = self.panel(**{"switchport access vlan": "% Invalid input detected at '^' marker."})
        panel.session.prompt = "SW(config-if)#"
        result = panel.apply("ports", "vlan", "Gi0/2", {"vlan": 30})
        self.assertFalse(result["ok"])
        self.assertIn("Invalid input", result["output"])
        self.assertEqual(panel.session.sent, ["configure terminal", "interface Gi0/2",
                                              "switchport mode access", "switchport access vlan 30", "end"])

    def test_log_lines_are_not_mistaken_for_errors(self):
        panel = self.panel(end="%SYS-5-CONFIG_I: Configured from console by netadmin on vty0")
        self.assertTrue(panel.apply("ports", "enable", "Gi0/3", {})["ok"])

    def test_user_mode_without_a_secret_is_view_only(self):
        panel = CiscoPanel(Scripted({"show interfaces status": STATUS}, prompt="SW>"))
        self.assertFalse(panel.can_change)
        self.assertEqual(panel.read("ports")["actions"], [])
        with self.assertRaises(PanelError):
            panel.plan("ports", "shutdown", "Gi0/2", {})
        self.assertEqual(panel.session.sent, ["show interfaces status"])

    def test_enable_secret_from_the_vault_is_used(self):
        session = Scripted(prompt="SW>")
        panel = CiscoPanel(session, "en-secret")
        self.assertTrue(panel.can_change)
        self.assertEqual(session.enabled_with, "en-secret")


TRUNKS = """
Port        Mode             Encapsulation  Status        Native vlan
Gi0/1       on               802.1q         trunking      99
Gi0/24      auto             n-802.1q       trunking      1

Port        Vlans allowed on trunk
Gi0/1       10,20,99
Gi0/24      1-4094

Port        Vlans allowed and active in management domain
Gi0/1       10,20
"""


class TestCiscoTrunks(unittest.TestCase):
    def panel(self, **answers):
        answers.setdefault("show interfaces status", STATUS)
        panel = CiscoPanel(Scripted(answers))
        panel.read("ports")
        panel.session.sent.clear()
        return panel

    def test_reading_takes_the_allowed_list_not_the_active_one(self):
        rows = {r["port"]: r for r in panels.parse_trunks(TRUNKS)}
        self.assertEqual(rows["Gi0/1"]["allowed"], "10,20,99")
        self.assertEqual((rows["Gi0/1"]["native"], rows["Gi0/1"]["mode"]), ("99", "on"))
        self.assertEqual(rows["Gi0/24"]["allowed"], "1-4094")

    def test_make_trunk(self):
        plan = self.panel().plan("ports", "trunk", "Gi0/2", {"vlans": "10, 20,30-35", "native": "99"})
        self.assertEqual(plan["commands"], [
            "configure terminal", "interface Gi0/2", "switchport trunk encapsulation dot1q",
            "switchport mode trunk", "switchport trunk allowed vlan 10,20,30-35",
            "switchport trunk native vlan 99", "end"])
        self.assertIn("access port", plan["warning"])

    def test_make_trunk_with_nothing_filled_in_allows_everything_and_says_so(self):
        plan = self.panel().plan("ports", "trunk", "Gi0/2", {})
        self.assertNotIn("allowed vlan", " ".join(plan["commands"]))
        self.assertIn("every VLAN is allowed", plan["warning"])

    def test_replacing_the_list_on_a_working_trunk_is_called_out(self):
        plan = self.panel().plan("ports", "trunk", "Gi0/1", {"vlans": "40"})
        self.assertIn("replaces the whole list", plan["warning"])

    def test_add_and_remove_never_replace_the_list(self):
        panel = self.panel()
        self.assertEqual(panel.plan("ports", "allow", "Gi0/1", {"vlans": "40"})["commands"][2],
                         "switchport trunk allowed vlan add 40")
        self.assertEqual(panel.plan("ports", "disallow", "Gi0/1", {"vlans": "40,50"})["commands"][2],
                         "switchport trunk allowed vlan remove 40,50")
        with self.assertRaises(PanelError):                       # an access port
            panel.plan("ports", "allow", "Gi0/2", {"vlans": "40"})
        with self.assertRaises(PanelError):                       # nothing to add
            panel.plan("ports", "allow", "Gi0/1", {"vlans": " "})

    def test_vlan_lists_are_checked(self):
        panel = self.panel()
        for bad in ("10;20", "10,,20", "all", "35-30", "0", "10-5000", "10 add 20", "10\nshutdown"):
            with self.assertRaises(PanelError, msg=bad):
                panel.plan("ports", "allow", "Gi0/1", {"vlans": bad})

    def test_a_switch_without_isl_rejects_encapsulation_and_the_rest_still_runs(self):
        panel = self.panel(**{"encapsulation": "% Invalid input detected at '^' marker."})
        result = panel.apply("ports", "trunk", "Gi0/2", {"vlans": "10,20"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "")
        self.assertEqual(panel.session.sent[-3:], ["switchport mode trunk",
                                                   "switchport trunk allowed vlan 10,20", "end"])

    def test_command_rejected_counts_as_a_failure(self):
        panel = self.panel(**{"switchport mode trunk":
                              'Command rejected: An interface whose trunk encapsulation is "Auto" can not be configured to "trunk" mode.'})
        result = panel.apply("ports", "trunk", "Gi0/2", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("switchport trunk native", " ".join(panel.session.sent))


SERVICES = """cron.service                loaded    active   running OpenBSD cron
nginx.service               loaded    failed   failed  A high performance web server
systemd-fsck-root.service   loaded    inactive dead    File System Check on Root Device
auditd.service              not-found inactive dead    auditd.service
ssh.service                 loaded    active   running OpenBSD Secure Shell server
"""


class TestLinux(unittest.TestCase):
    def panel(self, uid="1000", **answers):
        answers.setdefault("id -u", uid + "\n")
        answers.setdefault("systemctl list-units", SERVICES)
        return LinuxPanel(Scripted(answers))

    def test_services_put_failures_first_and_drop_missing_units(self):
        rows = panels.parse_services(SERVICES)
        self.assertEqual([r["unit"] for r in rows],
                         ["nginx.service", "cron.service", "ssh.service", "systemd-fsck-root.service"])
        self.assertEqual(rows[0]["description"], "A high performance web server")

    def test_listening(self):
        rows = panels.parse_listening(
            'tcp   LISTEN 0 128    0.0.0.0:22     0.0.0.0:*  users:(("sshd",pid=812,fd=3))\n'
            "udp   UNCONN 0 0      127.0.0.53%lo:53  0.0.0.0:*\n"
            'tcp   LISTEN 0 511       [::]:443       [::]:*  users:(("nginx",pid=9,fd=7),("nginx",pid=8,fd=7))\n')
        self.assertEqual(rows[0], {"protocol": "tcp", "address": "0.0.0.0", "port": "22", "program": "sshd"})
        self.assertEqual((rows[1]["address"], rows[1]["port"], rows[1]["program"]), ("[::]", "443", "nginx"))
        self.assertEqual((rows[2]["address"], rows[2]["program"]), ("127.0.0.53%lo", ""))

    def test_disks_routes_addresses(self):
        disks = panels.parse_df("Filesystem Type Size Used Avail Use% Mounted on\n"
                                "/dev/sda2 ext4 218G 189G 18G 92% /\n/dev/sdb1 ext4 1T 1G 1T 1% /mnt/my data\n")
        self.assertEqual((disks[0]["mount"], disks[0]["use"]), ("/", "92%"))
        self.assertEqual(disks[1]["mount"], "/mnt/my data")
        routes = panels.parse_ip_routes("default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.5 metric 100\n"
                                        "10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.5\n")
        self.assertEqual(routes[0], {"destination": "default", "gateway": "10.0.0.1", "interface": "eth0",
                                     "source": "10.0.0.5", "metric": "100"})
        self.assertEqual(routes[1]["gateway"], "")
        addresses = panels.parse_ip_addresses("lo UNKNOWN 127.0.0.1/8 ::1/128\neth0 UP 10.0.0.5/24\n")
        self.assertEqual(addresses[1], {"name": "eth0", "state": "Up", "addresses": "10.0.0.5/24"})

    def test_ordinary_users_go_through_sudo_and_root_does_not(self):
        user = self.panel()
        user.read("services")
        self.assertEqual(user.plan("services", "restart", "nginx.service", {})["commands"],
                         ["sudo -n systemctl restart nginx.service"])
        root = self.panel(uid="0")
        root.read("services")
        self.assertEqual(root.plan("services", "stop", "cron.service", {})["commands"],
                         ["systemctl stop cron.service"])

    def test_stopping_ssh_warns_about_being_locked_out(self):
        panel = self.panel()
        panel.read("services")
        self.assertIn("locks you out", panel.plan("services", "stop", "ssh.service", {})["warning"])

    def test_only_listed_services(self):
        panel = self.panel()
        panel.read("services")
        for row in ("nope.service", "cron.service; reboot", "auditd.service"):
            with self.assertRaises(PanelError):
                panel.plan("services", "restart", row, {})

    def test_failure_is_read_from_the_exit_status(self):
        panel = self.panel(**{"systemctl restart": "sudo: a password is required\n@@nettool@@1\n"})
        panel.read("services")
        result = panel.apply("services", "restart", "cron.service", {})
        self.assertFalse(result["ok"])
        self.assertIn("passwordless sudo", result["output"])
        ok = self.panel(**{"systemctl restart": "@@nettool@@0\n"})
        ok.read("services")
        self.assertEqual(ok.apply("services", "restart", "cron.service", {}), {"ok": True, "output": ""})


class TestOpenPanel(unittest.TestCase):
    def test_platform_and_port(self):
        made = []

        def factory(host, port, username, **kwargs):
            made.append((host, port, username, kwargs["password"]))
            return Scripted()

        device = {"platform": "mikrotik", "address": "10.0.0.1", "fields": {"mgmt_port": "2222"}}
        panel = open_panel(device, {"username": "admin", "password": "pw"}, factory)
        self.assertIsInstance(panel, MikrotikPanel)
        self.assertEqual(made, [("10.0.0.1", 2222, "admin", "pw")])

    def test_unsupported_platform(self):
        with self.assertRaises(PanelError):
            open_panel({"platform": "unknown", "address": "10.0.0.5"}, {}, lambda *a, **k: Scripted())


if __name__ == "__main__":
    unittest.main()
