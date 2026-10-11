"""Tests for monitoring: reading each platform's output, and the watch loop.

The device outputs are captured samples; no test opens a connection.
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nettool_engine import collectors
from nettool_engine.collectors import SEPARATOR, finish
from nettool_engine.history import History
from nettool_engine.monitor import Monitor
from nettool_engine.sshclient import SshError


def linux_output(busy, idle, rx, tx):
    return f"""cpu  {busy} 0 0 {idle} 0 0 0 0 0 0
{SEPARATOR}
MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    6000000 kB
{SEPARATOR}
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 111 1 0 0 0 0 0 0 111 1 0 0 0 0 0 0
  eth0: {rx} 10 0 0 0 0 0 0 {tx} 10 0 0 0 0 0 0
docker0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
{SEPARATOR}
86400.50 80000.00
{SEPARATOR}
docker0 down
eth0 up
lo unknown
{SEPARATOR}
default via 10.0.0.1 dev eth0 proto dhcp metric 100
"""


class TestLinux(unittest.TestCase):
    def test_reading(self):
        raw = collectors.parse_linux([linux_output(1000, 9000, 5000, 7000)])
        self.assertEqual(raw["cpu_ticks"], (1000, 10000))
        self.assertEqual(raw["mem_total"], 8000000 * 1024)
        # Available, not free: cache is not "used".
        self.assertEqual(raw["mem_used"], 2000000 * 1024)
        self.assertEqual(raw["uptime"], 86400.5)
        names = {i["name"]: i for i in raw["ifaces"]}
        self.assertNotIn("lo", names)
        self.assertTrue(names["eth0"]["main"] and names["eth0"]["up"])
        self.assertFalse(names["docker0"]["main"] or names["docker0"]["up"])

    def test_rates_come_from_two_readings(self):
        first = collectors.parse_linux([linux_output(1000, 9000, 5000, 7000)])
        second = collectors.parse_linux([linux_output(1300, 9700, 5000 + 12500, 7000 + 2500)])
        self.assertIsNone(finish(None, first, 0)["cpu"])       # nothing to compare with yet
        done = finish(first, second, 10.0)
        self.assertEqual(done["cpu"], 30.0)                    # 300 busy of 1000 ticks
        self.assertEqual(done["rx_bps"], 10000.0)              # 12500 bytes in 10 s
        self.assertEqual(done["tx_bps"], 2000.0)

    def test_a_counter_going_backwards_gives_no_rate(self):
        first = collectors.parse_linux([linux_output(1000, 9000, 5000, 7000)])
        rebooted = collectors.parse_linux([linux_output(1100, 9900, 10, 20)])
        done = finish(first, rebooted, 10.0)
        self.assertIsNone(done["rx_bps"])
        self.assertIsNone(done["ifaces"][0]["rx_bps"])

    def test_garbage_does_not_raise(self):
        raw = collectors.parse_linux(["command not found"])
        self.assertEqual(raw["ifaces"], [])
        self.assertIsNone(raw["cpu_ticks"])


class TestMikrotik(unittest.TestCase):
    OUTPUT = (
        "res,7,180000000,268435456,2w1d03:04:05\r\n"
        "if,ether1,ether,true,123456789,987654321\r\n"
        "if,bridge1,bridge,true,5,6\r\n"
        "if,uplink, backup,ether,false,0,0\r\n"
    )

    def test_reading(self):
        raw = collectors.parse_mikrotik([self.OUTPUT])
        self.assertEqual(raw["cpu"], 7.0)
        self.assertEqual(raw["mem_total"], 268435456)
        self.assertEqual(raw["mem_used"], 268435456 - 180000000)
        self.assertEqual(raw["uptime"], 2 * 604800 + 86400 + 3 * 3600 + 4 * 60 + 5)
        by_name = {i["name"]: i for i in raw["ifaces"]}
        self.assertTrue(by_name["ether1"]["main"])
        self.assertFalse(by_name["bridge1"]["main"])       # not counted twice
        self.assertIn("uplink, backup", by_name)           # a comma in the name survives
        self.assertFalse(by_name["uplink, backup"]["up"])

    def test_time_formats(self):
        self.assertEqual(collectors.parse_routeros_time("3h4m5s"), 11045.0)
        self.assertEqual(collectors.parse_routeros_time("00:00:09"), 9.0)
        self.assertIsNone(collectors.parse_routeros_time("soon"))
        self.assertIsNone(collectors.parse_routeros_time(""))


class TestCisco(unittest.TestCase):
    OUTPUTS = [
        "CPU utilization for five seconds: 12%/1%; one minute: 9%; five minutes: 8%\n",
        "Processor    2A7B1C0   736577964    88346540   648231424   640000000   600000000\n",
        "SW-FLOOR1 uptime is 3 weeks, 2 days, 4 hours, 15 minutes\n",
        """Vlan1 is up, line protocol is up 
  Hardware is EtherSVI, address is 0011.2233.4455
  5 minute input rate 1000 bits/sec, 1 packets/sec
  5 minute output rate 2000 bits/sec, 2 packets/sec
GigabitEthernet0/1 is up, line protocol is up (connected) 
  Hardware is Gigabit Ethernet, address is 0011.2233.4401
  5 minute input rate 48000 bits/sec, 20 packets/sec
  5 minute output rate 320000 bits/sec, 40 packets/sec
GigabitEthernet0/2 is down, line protocol is down (notconnect) 
  30 second input rate 0 bits/sec, 0 packets/sec
  30 second output rate 0 bits/sec, 0 packets/sec
GigabitEthernet0/3 is administratively down, line protocol is down (disabled) 
""",
    ]

    def test_reading(self):
        done = finish(None, collectors.parse_cisco(self.OUTPUTS), 0)
        self.assertEqual(done["cpu"], 12.0)
        self.assertEqual((done["mem_total"], done["mem_used"]), (736577964, 88346540))
        self.assertEqual(done["uptime"], 3 * 604800 + 2 * 86400 + 4 * 3600 + 15 * 60)
        by_name = {i["name"]: i for i in done["ifaces"]}
        self.assertEqual(len(by_name), 4)
        self.assertTrue(by_name["GigabitEthernet0/1"]["up"])
        self.assertFalse(by_name["GigabitEthernet0/2"]["up"])
        self.assertFalse(by_name["GigabitEthernet0/3"]["up"])
        # The SVI's traffic is already counted on the physical ports.
        self.assertFalse(by_name["Vlan1"]["main"])
        self.assertEqual((done["rx_bps"], done["tx_bps"]), (48000.0, 320000.0))

    def test_missing_answers(self):
        done = finish(None, collectors.parse_cisco(["% Invalid input", "", "", ""]), 0)
        self.assertIsNone(done["cpu"])
        self.assertIsNone(done["rx_bps"])


class FakeSession:
    opened = 0

    def __init__(self, host, port, username, password=None, private_key=None, passphrase=None):
        type(self).opened += 1
        self.args = (host, port, username, password)
        self.calls = 0
        self.closed = False

    def run(self, command):
        self.calls += 1
        return linux_output(1000 + 100 * self.calls, 9000 + 900 * self.calls,
                            5000 * self.calls, 7000 * self.calls)

    def close(self):
        self.closed = True


def device(device_id="d1", platform="linux", address="192.0.2.1"):
    return {"id": device_id, "title": device_id, "address": address, "platform": platform,
            "fields": {}}


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.arrived = threading.Condition()
        FakeSession.opened = 0

    def emit(self, message):
        with self.arrived:
            self.events.append(message)
            self.arrived.notify_all()

    def wait_for(self, count, device_id=None):
        def enough():
            return len(self.samples(device_id)) >= count
        with self.arrived:
            self.assertTrue(self.arrived.wait_for(enough, timeout=10))
        return self.samples(device_id)

    def samples(self, device_id=None):
        return [e["sample"] for e in self.events
                if e["event"] == "monitor.sample" and device_id in (None, e["device"])]

    def monitor(self, **kwargs):
        kwargs.setdefault("ping", lambda address: {"up": True, "ms": 1.5})
        kwargs.setdefault("session_factory", FakeSession)
        monitor = Monitor(self.emit, tick_seconds=0.05, **kwargs)
        self.addCleanup(monitor.stop)
        return monitor

    def test_ping_and_metrics(self):
        monitor = self.monitor()
        monitor.start([device()], lambda i: {"username": "root", "password": "pw"})
        first, second = self.wait_for(2)[:2]
        self.assertTrue(first["up"])
        self.assertEqual(first["ms"], 1.5)
        self.assertIsNone(first["cpu"])                 # needs two readings
        self.assertEqual(second["cpu"], 10.0)
        self.assertGreater(second["rx_bps"], 0)
        self.assertEqual(second["note"], "")
        self.assertEqual(FakeSession.opened, 1)         # one connection, reused

    def test_without_credentials_it_still_pings(self):
        def refuse(_id):
            raise RuntimeError("the vault is locked.")

        monitor = self.monitor()
        monitor.start([device()], refuse)
        sample = self.wait_for(1)[0]
        self.assertTrue(sample["up"])
        self.assertIn("vault is locked", sample["note"])
        self.assertEqual(FakeSession.opened, 0)

    def test_unsupported_platform_is_ping_only_and_asks_for_no_secret(self):
        asked = []
        monitor = self.monitor()
        monitor.start([device(platform="unknown")], asked.append)
        self.assertIn("Ping only", self.wait_for(1)[0]["note"])
        self.assertEqual(asked, [])

    def test_failed_sign_in_is_reported_and_not_hammered(self):
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(1)
            raise SshError("Sign-in was refused.")

        monitor = self.monitor(session_factory=refuse)
        monitor.start([device()], lambda i: {"username": "root", "password": "bad"})
        samples = self.wait_for(6)
        self.assertEqual(samples[0]["note"], "Sign-in was refused.")
        self.assertTrue(all(s["up"] for s in samples))  # ping carries on regardless
        self.assertEqual(len(attempts), 1)

    def test_start_replaces_the_watched_set(self):
        monitor = self.monitor()
        credentials = lambda i: {"username": "u", "password": "p"}
        monitor.start([device("a"), device("b")], credentials)
        self.wait_for(1, "a"), self.wait_for(1, "b")
        state = monitor.start([device("b"), device("c")], credentials)
        self.assertEqual(sorted(w["device"]["id"] for w in state["watching"]), ["b", "c"])
        self.wait_for(1, "c")
        time.sleep(0.2)
        before = len(self.samples("a"))
        time.sleep(0.3)
        self.assertEqual(len(self.samples("a")), before)   # "a" really stopped

    def test_state_keeps_the_series_without_interface_lists(self):
        monitor = self.monitor()
        monitor.start([device()], lambda i: {"username": "u", "password": "p"})
        live = self.wait_for(2)
        self.assertIsNotNone(live[1]["ifaces"])
        kept = monitor.state()["watching"][0]["samples"]
        self.assertGreaterEqual(len(kept), 2)
        self.assertTrue(all(s["ifaces"] is None for s in kept))

    def test_samples_reach_history(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(os.path.join(directory, "h.db"))
            monitor = self.monitor(history=history,
                                   ping=lambda a: {"up": a != "192.0.2.9", "ms": 2.0 if a != "192.0.2.9" else None})
            monitor.start([device("up"), device("down", address="192.0.2.9")],
                          lambda i: {"username": "u", "password": "p"})
            self.wait_for(3, "up"), self.wait_for(3, "down")
            monitor.stop()
            time.sleep(0.2)
            rows = {r["device"]: r for r in history.summary(1)}
            self.assertEqual(rows["up"]["availability"], 100.0)
            self.assertEqual(rows["down"]["availability"], 0.0)
            self.assertEqual(rows["up"]["ms_avg"], 2.0)
            path = history.export_csv(1, directory)
            with open(path, encoding="utf-8-sig") as fh:
                lines = fh.read().splitlines()
            self.assertTrue(lines[0].startswith("device,address,time,answered_ping"))
            self.assertGreaterEqual(len(lines), 7)
            history.close()


if __name__ == "__main__":
    unittest.main()
