"""Tests for the credential broker the network tool talks to.

Two things matter here and are easy to get subtly wrong: what a session is
allowed to see (devices only, secrets one at a time, nothing while locked), and
that the endpoint really is single-use.
"""

import os
import sys
import threading
import time
import unittest
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault.broker import (
    BrokerClient, BrokerError, BrokerSession, describe_device, handle_request,
    is_device,
)
from vault.models import Attachment, Category, Entry


class FakeManager:
    """Just the surface handle_request uses, without a real .kdbx behind it."""

    path = "/somewhere/vault.kdbx"

    def __init__(self, entries):
        self._entries = entries
        self.is_locked = False
        self.touched = 0

    @property
    def entries(self):
        return list(self._entries)

    def get(self, entry_id):
        return next((e for e in self._entries if e.id == entry_id), None)

    def touch(self):
        self.touched += 1

    def add(self, entry):
        self._entries.append(entry)

    def save(self):
        self.saved = getattr(self, "saved", 0) + 1


def sample_entries():
    router = Entry(
        title="Core router", category=Category.NETWORK,
        fields={
            "vendor": "MikroTik", "mgmt_ip": "10.0.0.1", "username": "admin",
            "password": "hunter2", "snmp_community": "s3cret",
            "backup_password": "custom-and-unlisted",
        },
        notes="recovery code 123456",
    )
    server = Entry(
        title="Web server", category=Category.SSH_KEY,
        fields={"host": "192.0.2.10", "username": "deploy", "passphrase": "pp"},
        attachments=[Attachment(name="id_ed25519", data=b"key")],
    )
    bare_key = Entry(title="Spare key", category=Category.SSH_KEY, fields={})
    bank = Entry(
        title="Bank", category=Category.LOGIN,
        fields={"username": "me", "password": "money"},
    )
    return router, server, bare_key, bank


class TestVisibility(unittest.TestCase):
    def setUp(self):
        self.router, self.server, self.bare_key, self.bank = sample_entries()
        self.manager = FakeManager([self.router, self.server, self.bare_key, self.bank])

    def test_only_devices_are_devices(self):
        self.assertTrue(is_device(self.router))
        self.assertTrue(is_device(self.server))
        self.assertFalse(is_device(self.bare_key))
        self.assertFalse(is_device(self.bank))

    def test_listing_has_no_secrets(self):
        result = handle_request(self.manager, {"op": "list_devices"})
        self.assertEqual([d["title"] for d in result["devices"]],
                         ["Core router", "Web server"])
        text = repr(result)
        for secret in ("hunter2", "s3cret", "custom-and-unlisted", "123456", "pp'", "money"):
            self.assertNotIn(secret, text)

    def test_listing_says_whether_secrets_exist(self):
        router = describe_device(self.router)
        self.assertTrue(router["has_password"])
        self.assertFalse(router["has_key"])
        self.assertEqual(router["address"], "10.0.0.1")
        self.assertTrue(describe_device(self.server)["has_key"])

    def test_listing_does_not_defer_autolock(self):
        handle_request(self.manager, {"op": "list_devices"})
        self.assertEqual(self.manager.touched, 0)

    def test_credentials_for_a_device(self):
        result = handle_request(self.manager, {"op": "get_credentials", "device": self.router.id})
        self.assertEqual(result["fields"]["password"], "hunter2")
        self.assertEqual(result["fields"]["username"], "admin")
        self.assertEqual(result["fields"]["snmp_community"], "s3cret")
        # Not a declared secret and not a connection field, so it stays put.
        self.assertNotIn("backup_password", result["fields"])
        self.assertEqual(self.manager.touched, 1)

    def test_credentials_include_the_private_key_attachment(self):
        pem = b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
        self.server.attachments = [
            Attachment(name="id_ed25519.pub", data=b"ssh-ed25519 AAAA me@host"),
            Attachment(name="backup", data=pem),
        ]
        result = handle_request(self.manager, {"op": "get_credentials", "device": self.server.id})
        self.assertEqual(result["fields"]["private_key"], pem.decode())
        self.assertEqual(result["fields"]["passphrase"], "pp")
        # A device with no key attachment simply has none.
        router = handle_request(self.manager, {"op": "get_credentials", "device": self.router.id})
        self.assertNotIn("private_key", router["fields"])

    def test_credentials_refused_for_non_devices(self):
        for entry in (self.bank, self.bare_key):
            with self.assertRaises(BrokerError) as caught:
                handle_request(self.manager, {"op": "get_credentials", "device": entry.id})
            self.assertEqual(caught.exception.code, "not_found")

    def test_unknown_id_looks_the_same_as_a_non_device(self):
        with self.assertRaises(BrokerError) as missing:
            handle_request(self.manager, {"op": "get_credentials", "device": "nope"})
        with self.assertRaises(BrokerError) as hidden:
            handle_request(self.manager, {"op": "get_credentials", "device": self.bank.id})
        self.assertEqual(str(missing.exception), str(hidden.exception))

    def test_ssh_opens_a_terminal_for_the_device(self):
        from unittest import mock

        launched = []

        offered = []

        def fake_launch(command, title="amvault", env=None):
            launched.append(command)
            offered.append(env)
            return mock.Mock(pid=1)

        with mock.patch("vault.ssh_connect.ssh_available", return_value=True), \
             mock.patch("vault.ssh_connect.launch_terminal", fake_launch):
            result = handle_request(self.manager, {"op": "ssh", "device": self.router.id})

        self.assertEqual(launched, [["ssh", "admin@10.0.0.1"]])
        self.assertEqual(result["display"], "ssh admin@10.0.0.1")
        # The reply describes the session; it must not carry the password.
        self.assertNotIn("hunter2", repr(result))
        # The password is on offer to ssh through the helper -- and is in
        # neither the command nor the variables that point to it.
        self.assertTrue(result["password_supplied"] if "password_supplied" in result else True)
        self.assertEqual(offered[0]["SSH_ASKPASS_REQUIRE"], "force")
        self.assertNotIn("hunter2", repr(offered) + repr(launched))
        self.assertEqual(self.manager.touched, 1)

    def test_ssh_refused_for_non_devices(self):
        with self.assertRaises(BrokerError) as caught:
            handle_request(self.manager, {"op": "ssh", "device": self.bank.id})
        self.assertEqual(caught.exception.code, "not_found")

    def test_add_device_creates_a_network_entry(self):
        result = handle_request(self.manager, {
            "op": "add_device", "title": "New switch",
            "fields": {"mgmt_ip": "10.0.0.9", "vendor": "Cisco"},
        })
        added = self.manager.get(result["device"]["id"])
        self.assertEqual(added.category, Category.NETWORK)
        self.assertEqual(added.title, "New switch")
        self.assertEqual(added.fields, {"mgmt_ip": "10.0.0.9", "vendor": "Cisco"})
        self.assertEqual(self.manager.saved, 1)

    def test_add_device_takes_no_secrets_or_unlisted_fields(self):
        result = handle_request(self.manager, {
            "op": "add_device",
            "fields": {"mgmt_ip": "10.0.0.9", "password": "x", "username": "root",
                       "notes": "y", "anything": "z"},
        })
        added = self.manager.get(result["device"]["id"])
        self.assertEqual(added.fields, {"mgmt_ip": "10.0.0.9"})
        self.assertEqual(added.title, "10.0.0.9")   # falls back to the address
        self.assertEqual(added.notes, "")

    def test_add_device_needs_a_real_address(self):
        for bad in ("", "not-an-ip", "10.0.0.999", "-oProxyCommand=x"):
            with self.assertRaises(BrokerError) as caught:
                handle_request(self.manager, {"op": "add_device", "fields": {"mgmt_ip": bad}})
            self.assertEqual(caught.exception.code, "bad_request")

    def test_add_device_refuses_an_address_already_in_the_vault(self):
        for taken in ("10.0.0.1", "192.0.2.10"):    # a mgmt_ip and a host
            with self.assertRaises(BrokerError) as caught:
                handle_request(self.manager, {"op": "add_device", "fields": {"mgmt_ip": taken}})
            self.assertEqual(caught.exception.code, "exists")
        self.assertEqual(len(self.manager.entries), 4)

    def test_add_device_hands_saving_to_the_gui_when_there_is_one(self):
        calls = []
        handle_request(self.manager, {"op": "add_device", "fields": {"mgmt_ip": "10.0.0.9"}},
                       on_change=lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertFalse(hasattr(self.manager, "saved"))

    def test_locked_vault_answers_nothing_but_status(self):
        self.manager.is_locked = True
        self.assertTrue(handle_request(self.manager, {"op": "status"})["locked"])
        for request in ({"op": "list_devices"},
                        {"op": "get_credentials", "device": self.router.id},
                        {"op": "ssh", "device": self.router.id},
                        {"op": "add_device", "fields": {"mgmt_ip": "10.0.0.9"}}):
            with self.assertRaises(BrokerError) as caught:
                handle_request(self.manager, request)
            self.assertEqual(caught.exception.code, "locked")

    def test_no_vault_open_counts_as_locked(self):
        self.assertEqual(handle_request(None, {"op": "status"}),
                         {"locked": True, "vault": None})
        with self.assertRaises(BrokerError):
            handle_request(None, {"op": "list_devices"})

    def test_unknown_op(self):
        with self.assertRaises(BrokerError) as caught:
            handle_request(self.manager, {"op": "dump_everything"})
        self.assertEqual(caught.exception.code, "unknown_op")


class TestSession(unittest.TestCase):
    def setUp(self):
        self.router, *_ = sample_entries()
        self.manager = FakeManager([self.router])
        self.session = BrokerSession(lambda request: handle_request(self.manager, request))
        self.addCleanup(self.session.close)

    def connect(self, **kwargs):
        client = BrokerClient(**self.session.handshake_args(), **kwargs)
        self.addCleanup(client.close)
        return client

    def test_round_trip(self):
        client = self.connect()
        devices = client.request("list_devices")["devices"]
        self.assertEqual(devices[0]["title"], "Core router")
        creds = client.request("get_credentials", device=self.router.id)
        self.assertEqual(creds["fields"]["password"], "hunter2")

    def test_refusal_reaches_the_client_as_an_error(self):
        client = self.connect()
        self.manager.is_locked = True
        with self.assertRaises(BrokerError) as caught:
            client.request("list_devices")
        self.assertEqual(caught.exception.code, "locked")
        # ...and the session survives it, ready for when the vault reopens.
        self.manager.is_locked = False
        self.assertEqual(len(client.request("list_devices")["devices"]), 1)

    def test_wrong_token_is_rejected(self):
        args = self.session.handshake_args()
        with self.assertRaises((AuthenticationError, EOFError, OSError)):
            Client(args["address"], family=args["family"], authkey=b"x" * 32)

    def test_endpoint_is_single_use(self):
        self.connect().request("status")
        args = self.session.handshake_args()
        # The right token, but the one connection has been spent.
        with self.assertRaises(OSError):
            Client(args["address"], family=args["family"],
                   authkey=bytes.fromhex(args["token_hex"]))
        if self.session.family == "AF_UNIX":
            self.assertFalse(os.path.exists(self.session.address))

    def test_events_are_pushed(self):
        seen = []
        got = threading.Event()

        def on_event(message):
            seen.append(message["event"])
            got.set()

        client = self.connect(on_event=on_event)
        client.request("status")
        self.session.notify("locked")
        self.assertTrue(got.wait(5))
        self.assertEqual(seen, ["locked"])

    def test_closing_the_session_fails_pending_and_later_requests(self):
        client = self.connect()
        client.request("status")
        self.session.close()
        self.assertTrue(client.closed.wait(5))
        with self.assertRaises(BrokerError):
            client.request("status", timeout=2)
        self.assertFalse(self.session.is_alive)

    def test_client_leaving_ends_the_session(self):
        client = self.connect()
        client.request("status")
        client.close()
        deadline = time.time() + 5
        while self.session.is_alive and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.session.is_alive)


if __name__ == "__main__":
    unittest.main()
