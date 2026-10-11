"""Tests for turning an entry into an ssh command.

Entries are filled in by hand, so the host field arrives in whatever shape the
user pasted: bare, with a user prefix, with a port, or as a URL. These pin down
that each of those still produces a correct command.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault.models import Category, Entry
from vault.ssh_connect import (
    SshConnectError, _write_public_key, build_ssh_command, describe_target,
)


def cmd(**fields):
    return build_ssh_command(Entry(title="T", fields=fields))


class TestCommandBuilding(unittest.TestCase):
    def test_host_and_user(self):
        self.assertEqual(cmd(host="10.0.0.5", username="root"),
                         ["ssh", "root@10.0.0.5"])

    def test_host_without_user(self):
        self.assertEqual(cmd(host="example.com"), ["ssh", "example.com"])

    def test_custom_port(self):
        self.assertEqual(cmd(host="h", username="u", port="2222"),
                         ["ssh", "-p", "2222", "u@h"])

    def test_default_port_is_omitted(self):
        """Passing -p 22 works but is noise; leave it out."""
        self.assertEqual(cmd(host="h", username="u", port="22"), ["ssh", "u@h"])

    def test_network_device_fields(self):
        self.assertEqual(cmd(mgmt_ip="192.168.1.1", username="admin", mgmt_port="2200"),
                         ["ssh", "-p", "2200", "admin@192.168.1.1"])

    def test_user_embedded_in_host(self):
        self.assertEqual(cmd(host="deploy@srv.io"), ["ssh", "deploy@srv.io"])

    def test_explicit_username_wins_over_embedded(self):
        self.assertEqual(cmd(host="wrong@srv.io", username="right"),
                         ["ssh", "right@srv.io"])

    def test_ssh_url_prefix_stripped(self):
        self.assertEqual(cmd(host="ssh://box.local", username="me"),
                         ["ssh", "me@box.local"])

    def test_port_pasted_into_host(self):
        self.assertEqual(cmd(host="srv.io:2022", username="me"),
                         ["ssh", "-p", "2022", "me@srv.io"])

    def test_ipv6_literal_is_not_split_as_port(self):
        """A bracketed IPv6 address contains colons that are not a port."""
        result = cmd(host="[2001:db8::1]", username="me")
        self.assertEqual(result, ["ssh", "me@[2001:db8::1]"])

    def test_trailing_slash_stripped(self):
        self.assertEqual(cmd(host="box.local/", username="me"),
                         ["ssh", "me@box.local"])

    def test_missing_host_raises_with_guidance(self):
        with self.assertRaises(SshConnectError) as ctx:
            cmd(username="root")
        self.assertIn("host", str(ctx.exception).lower())

    def test_blank_host_raises(self):
        with self.assertRaises(SshConnectError):
            cmd(host="   ", username="root")


class TestKeyScoping(unittest.TestCase):
    """ssh-agent offers every key it holds; a server with several loaded can hit
    MaxAuthTries before reaching the right one. Scoping to one key avoids that."""

    @classmethod
    def setUpClass(cls):
        import subprocess, tempfile
        cls.dir = tempfile.TemporaryDirectory()
        path = os.path.join(cls.dir.name, "k")
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", path, "-q"],
                       check=True, capture_output=True)
        with open(path, "rb") as fh:
            cls.private = fh.read()
        with open(path + ".pub") as fh:
            cls.expected_public = fh.read().split()[:2]

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_public_key_derived_correctly(self):
        path = _write_public_key(self.private, None)
        self.assertIsNotNone(path)
        with open(path) as fh:
            self.assertEqual(fh.read().split()[:2], self.expected_public)

    def test_private_copy_is_not_left_behind(self):
        path = _write_public_key(self.private, None)
        self.assertFalse(os.path.exists(path[:-4]),
                         "the private key must not survive extraction")

    def test_invalid_key_returns_none(self):
        self.assertIsNone(_write_public_key(b"not a key at all", None))

    def test_command_scopes_to_the_key(self):
        e = Entry(title="T", fields={"host": "h", "username": "u"})
        command = build_ssh_command(e, "/tmp/x.pub")
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("IdentityFile=/tmp/x.pub", command)

    def test_command_without_a_key_is_unscoped(self):
        e = Entry(title="T", fields={"host": "h", "username": "u"})
        self.assertEqual(build_ssh_command(e), ["ssh", "u@h"])


class TestDescribeTarget(unittest.TestCase):
    def test_includes_port(self):
        e = Entry(title="T", fields={"host": "h", "username": "u", "port": "2222"})
        self.assertEqual(describe_target(e), "u@h:2222")

    def test_plain(self):
        e = Entry(title="T", fields={"host": "h", "username": "u"})
        self.assertEqual(describe_target(e), "u@h")

    def test_unusable_entry_does_not_raise(self):
        """The UI calls this while rendering; it must never throw."""
        self.assertEqual(describe_target(Entry(title="T", fields={})), "?")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestDefaultTerminal(unittest.TestCase):
    """x-terminal-emulator is a symlink; what it points at decides the flag."""

    def resolve(self, target):
        from vault import ssh_connect
        with mock.patch("os.path.realpath", return_value=target):
            return ssh_connect._resolve_alternative("/usr/bin/x-terminal-emulator")

    def test_a_terminal_itself_gets_its_own_flag(self):
        self.assertEqual(self.resolve("/usr/bin/kitty"), ("/usr/bin/x-terminal-emulator", []))
        self.assertEqual(self.resolve("/usr/bin/konsole"), ("/usr/bin/x-terminal-emulator", ["-e"]))

    def test_a_debian_wrapper_is_spoken_to_as_xterm(self):
        # The wrapper only translates -e; given gnome-terminal's own "--" it
        # opens an empty window and the ssh command is lost.
        self.assertEqual(self.resolve("/usr/bin/gnome-terminal.wrapper"),
                         ("/usr/bin/x-terminal-emulator", ["-e"]))
        self.assertEqual(self.resolve("/usr/bin/xfce4-terminal.wrapper"),
                         ("/usr/bin/x-terminal-emulator", ["-e"]))

    def test_an_unknown_target_is_left_to_the_table(self):
        self.assertIsNone(self.resolve("/opt/strange/term"))
