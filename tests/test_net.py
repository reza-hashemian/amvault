"""Tests for reaching and finding hosts.

The parsers are tested against captured output from each platform, since the
machine running the tests only has one of them.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nettool_engine import net


class TestSafeTarget(unittest.TestCase):
    def test_accepts_addresses_and_hostnames(self):
        self.assertEqual(net.safe_target(" 10.0.0.1 "), "10.0.0.1")
        self.assertEqual(net.safe_target("sw-floor1.office.lan"), "sw-floor1.office.lan")
        self.assertEqual(net.safe_target("::1"), "::1")

    def test_refuses_anything_ping_could_read_as_an_option(self):
        for bad in ("-c 5", "-f", "", "a b", "10.0.0.1; rm -rf ~", "$(reboot)", "host/24"):
            with self.assertRaises(net.NetError):
                net.safe_target(bad)

    def test_ping_of_a_bad_target_is_just_down(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(net.ping("-f"), {"up": False, "ms": None})
            run.assert_not_called()


class TestParsePing(unittest.TestCase):
    LINUX = "64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.412 ms\n"
    WINDOWS = "Reply from 10.0.0.1: bytes=32 time<1ms TTL=64\n"
    WINDOWS_UNREACHABLE = "Reply from 10.0.0.5: Destination host unreachable.\n"

    def test_linux_reply(self):
        self.assertEqual(net.parse_ping(self.LINUX, 0, 9.0), {"up": True, "ms": 0.4})

    def test_no_reply(self):
        self.assertEqual(net.parse_ping("", 1, 1000.0), {"up": False, "ms": None})

    def test_unparseable_time_falls_back_to_the_clock(self):
        self.assertEqual(net.parse_ping("Antwort erhalten", 0, 12.34), {"up": True, "ms": 12.3})

    def test_windows_unreachable_is_down_despite_exit_zero(self):
        with mock.patch.object(net, "IS_WINDOWS", True):
            self.assertFalse(net.parse_ping(self.WINDOWS_UNREACHABLE, 0, 5.0)["up"])
            self.assertEqual(net.parse_ping(self.WINDOWS, 0, 5.0), {"up": True, "ms": 1.0})


class TestNetworks(unittest.TestCase):
    IP_JSON = """[
      {"ifname": "lo", "flags": ["LOOPBACK", "UP"],
       "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
      {"ifname": "eno1", "flags": ["BROADCAST", "UP"],
       "addr_info": [{"family": "inet", "local": "10.10.90.247", "prefixlen": 24}]},
      {"ifname": "docker0", "flags": ["BROADCAST", "UP"],
       "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]}
    ]"""

    def test_parse_ip_json_skips_loopback(self):
        networks = net.parse_ip_json(self.IP_JSON)
        self.assertEqual([(n["interface"], n["cidr"]) for n in networks],
                         [("eno1", "10.10.90.0/24"), ("docker0", "172.17.0.0/16")])

    def test_a_large_network_is_offered_as_the_slash_24_we_are_in(self):
        self.assertEqual(net._suggest_range({"cidr": "172.17.0.0/16", "address": "172.17.3.9"}),
                         "172.17.3.0/24")
        self.assertEqual(net._suggest_range({"cidr": "10.10.88.0/22", "address": "10.10.90.2"}),
                         "10.10.88.0/22")

    def test_scan_range_limits(self):
        self.assertEqual(str(net.scan_range("192.168.1.77/24")), "192.168.1.0/24")
        for bad in ("10.0.0.0/8", "nonsense", "", "fe80::/64"):
            with self.assertRaises(net.NetError):
                net.scan_range(bad)


class TestNeighbours(unittest.TestCase):
    def test_linux(self):
        text = (
            "10.10.90.1 dev eno1 lladdr 4c:5e:0c:12:34:56 REACHABLE\n"
            "10.10.90.7 dev eno1 FAILED\n"
            "10.10.90.9 dev eno1 lladdr 00:1b:54:aa:bb:cc STALE\n"
        )
        self.assertEqual(net.parse_neighbours(text), {
            "10.10.90.1": "4c:5e:0c:12:34:56", "10.10.90.9": "00:1b:54:aa:bb:cc",
        })

    def test_windows(self):
        text = (
            "Interface: 192.168.1.20 --- 0x5\n"
            "  Internet Address      Physical Address      Type\n"
            "  192.168.1.1           4c-5e-0c-12-34-56     dynamic\n"
            "  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n"
            "  224.0.0.22            01-00-5e-00-00-16     static\n"
        )
        self.assertEqual(net.parse_neighbours(text), {"192.168.1.1": "4c:5e:0c:12:34:56"})

    def test_macos_short_octets(self):
        text = "? (192.168.1.1) at 4c:5e:c:12:34:56 on en0 ifscope [ethernet]\n? (192.168.1.9) at (incomplete) on en0\n"
        self.assertEqual(net.parse_neighbours(text), {"192.168.1.1": "4c:5e:0c:12:34:56"})


class TestSshBanner(unittest.TestCase):
    def serve(self, greeting: bytes) -> int:
        import socket
        import threading

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def answer():
            try:
                connection, _ = listener.accept()
                if greeting:
                    connection.sendall(greeting)
                connection.close()
            except OSError:
                pass

        threading.Thread(target=answer, daemon=True).start()
        return listener.getsockname()[1]

    def test_reads_the_greeting_without_sending_anything(self):
        port = self.serve(b"SSH-2.0-ROSSSH\r\nmore bytes that are not part of it")
        self.assertEqual(net.ssh_banner("127.0.0.1", port), "SSH-2.0-ROSSSH")

    def test_something_that_is_not_ssh_or_not_there(self):
        self.assertEqual(net.ssh_banner("127.0.0.1", self.serve(b"HTTP/1.1 400 Bad Request\r\n")), "")
        self.assertEqual(net.ssh_banner("127.0.0.1", self.serve(b"")), "")
        self.assertEqual(net.ssh_banner("-oProxyCommand=x", 22), "")

    def test_platform_from_greeting(self):
        from nettool_engine import platforms
        cases = {
            "SSH-2.0-ROSSSH": "mikrotik", "SSH-2.0-Cisco-1.25": "cisco_ios",
            "SSH-2.0-OpenSSH_for_Windows_9.5": "windows",
            "SSH-2.0-OpenSSH_9.2p1 Debian-2": "linux", "SSH-2.0-Weird": "unknown", "": "unknown",
        }
        for greeting, platform in cases.items():
            self.assertEqual(platforms.from_ssh_banner(greeting), platform, greeting)


class TestGuess(unittest.TestCase):
    def test_only_telling_ports_decide(self):
        self.assertEqual(net.guess_platform([22, 80, 8291]), "mikrotik")
        self.assertEqual(net.guess_platform([445, 3389]), "windows")
        self.assertEqual(net.guess_platform([22]), "")
        self.assertEqual(net.guess_platform([]), "")


class TestScanner(unittest.TestCase):
    def run_scan(self, alive, table):
        events = []
        done = __import__("threading").Event()

        def emit(message):
            events.append(message)
            if message["event"] == "scan.done":
                done.set()

        with mock.patch.object(net, "ping", lambda a: {"up": a in alive, "ms": 1.0 if a in alive else None}), \
             mock.patch.object(net, "open_ports", lambda a: [22] if a.endswith(".1") else []), \
             mock.patch.object(net, "hostname_of", lambda a: ""), \
             mock.patch.object(net, "neighbours", lambda: table):
            scanner = net.Scanner(emit)
            started = scanner.start("192.0.2.0/29")
            self.assertTrue(done.wait(10))
        hosts = {}
        for event in events:
            if event["event"] == "scan.host":
                hosts[event["host"]["address"]] = event["host"]
        return started, events, hosts, scanner

    def test_finds_answering_and_silent_hosts(self):
        started, events, hosts, _ = self.run_scan(
            alive={"192.0.2.1", "192.0.2.3"},
            table={"192.0.2.1": "aa:aa:aa:aa:aa:01", "192.0.2.5": "aa:aa:aa:aa:aa:05",
                   "198.51.100.9": "aa:aa:aa:aa:aa:09"},   # outside the range
        )
        self.assertEqual(started, {"cidr": "192.0.2.0/29", "total": 6})
        self.assertEqual(sorted(hosts), ["192.0.2.1", "192.0.2.3", "192.0.2.5"])
        self.assertEqual(hosts["192.0.2.1"]["mac"], "aa:aa:aa:aa:aa:01")
        self.assertEqual(hosts["192.0.2.1"]["ports"], [22])
        self.assertIsNone(hosts["192.0.2.5"]["ms"])      # seen only by ARP
        self.assertEqual(events[-1], {"event": "scan.done", "cancelled": False, "found": 3})
        progress = [e for e in events if e["event"] == "scan.progress"]
        self.assertEqual(progress[-1]["done"], 6)

    def test_a_finished_scan_can_be_followed_by_another(self):
        *_, scanner = self.run_scan(alive=set(), table={})
        with mock.patch.object(net, "ping", lambda a: {"up": False, "ms": None}), \
             mock.patch.object(net, "neighbours", lambda: {}):
            self.assertEqual(scanner.start("192.0.2.0/30")["total"], 2)


if __name__ == "__main__":
    unittest.main()
