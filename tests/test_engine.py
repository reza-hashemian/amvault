"""Tests for the engine: platform detection and the JSON-lines protocol."""

import json
import os
import subprocess
import sys
import unittest

import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.TemporaryDirectory()
sys.path.insert(0, ROOT)

from nettool_engine import platforms


def device(**fields):
    title = fields.pop("title", "x")
    tags = fields.pop("tags", [])
    return {"title": title, "tags": tags, "fields": fields}


class TestPlatformDetection(unittest.TestCase):
    def test_by_vendor(self):
        self.assertEqual(platforms.detect(device(vendor="MikroTik")), platforms.MIKROTIK)
        self.assertEqual(platforms.detect(device(vendor="Cisco")), platforms.CISCO_IOS)
        self.assertEqual(platforms.detect(device(vendor="Microsoft")), platforms.WINDOWS)

    def test_by_model_or_firmware_when_vendor_is_missing(self):
        self.assertEqual(platforms.detect(device(model="Catalyst 2960X")), platforms.CISCO_IOS)
        self.assertEqual(platforms.detect(device(firmware="RouterOS 7.15")), platforms.MIKROTIK)
        self.assertEqual(platforms.detect(device(model="CCR2004-16G-2S+")), platforms.MIKROTIK)

    def test_by_title_or_tag(self):
        self.assertEqual(platforms.detect(device(title="Ubuntu web server")), platforms.LINUX)
        self.assertEqual(platforms.detect(device(tags=["linux"])), platforms.LINUX)
        self.assertEqual(platforms.detect(device(title="Windows file server")), platforms.WINDOWS)

    def test_ios_needs_a_word_boundary(self):
        # "ios" inside another word must not turn a device into a Cisco one.
        self.assertEqual(platforms.detect(device(title="Radios cabinet")), platforms.UNKNOWN)
        self.assertEqual(platforms.detect(device(firmware="IOS 15.2")), platforms.CISCO_IOS)

    def test_a_bare_ssh_key_entry_is_taken_for_a_linux_server(self):
        self.assertEqual(platforms.detect({"category": "ssh_key", "title": "web-01", "fields": {}}),
                         platforms.LINUX)
        # ...unless the entry says otherwise.
        self.assertEqual(platforms.detect({"category": "ssh_key", "title": "x",
                                           "fields": {"vendor": "MikroTik"}}), platforms.MIKROTIK)

    def test_unknown(self):
        self.assertEqual(platforms.detect(device(vendor="Fortinet")), platforms.UNKNOWN)
        self.assertEqual(platforms.detect({}), platforms.UNKNOWN)


def run_engine(requests, demo=False, handshake="{}"):
    env = dict(os.environ)
    env.pop("NETTOOL_DEMO", None)
    # Keep the engine's history file out of the real data directory.
    env["NETTOOL_DATA_DIR"] = DATA_DIR.name
    if demo:
        env["NETTOOL_DEMO"] = "1"
    lines = [handshake] + [json.dumps(r) for r in requests]
    done = subprocess.run(
        [sys.executable, "-m", "nettool_engine"],
        input="\n".join(lines) + "\n", capture_output=True, text=True,
        cwd=ROOT, env=env, timeout=30,
    )
    replies = {}
    for line in done.stdout.splitlines():
        message = json.loads(line)   # every stdout line must be protocol
        replies[message.get("id")] = message
    return replies


class TestProtocol(unittest.TestCase):
    def test_without_a_vault_it_says_so(self):
        replies = run_engine([
            {"id": 1, "method": "app.status"},
            {"id": 2, "method": "devices.list"},
        ])
        self.assertEqual(replies[1]["result"]["source"], "none")
        self.assertIn("amvault", replies[2]["error"])

    def test_demo_devices_carry_a_platform(self):
        replies = run_engine([
            {"id": 1, "method": "app.status"},
            {"id": 2, "method": "devices.list"},
        ], demo=True)
        self.assertEqual(replies[1]["result"]["source"], "demo")
        devices = replies[2]["result"]["devices"]
        self.assertTrue(devices)
        self.assertTrue(all("platform" in d for d in devices))

    def test_demo_refuses_to_connect(self):
        replies = run_engine([{"id": 1, "method": "ssh.open", "params": {"device": "demo-1"}}],
                             demo=True)
        self.assertIn("sample", replies[1]["error"])

    def test_unknown_method_and_garbage_do_not_kill_it(self):
        env_requests = [{"id": 1, "method": "no.such"}, {"id": 2, "method": "app.status"}]
        done = run_engine(env_requests, handshake="not json")
        self.assertIn("Unknown method", done[1]["error"])
        self.assertIn("result", done[2])

    def test_unreachable_broker_degrades_to_no_vault(self):
        handshake = json.dumps({"broker": {
            "address": os.path.join(ROOT, "no-such-socket"), "family": "AF_UNIX", "token": "00" * 32,
        }})
        if sys.platform == "win32":
            self.skipTest("AF_UNIX endpoint")
        replies = run_engine([{"id": 1, "method": "app.status"}], handshake=handshake)
        self.assertEqual(replies[1]["result"]["source"], "none")


if __name__ == "__main__":
    unittest.main()
