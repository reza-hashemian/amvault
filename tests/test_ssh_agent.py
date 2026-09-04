"""Tests for the ssh-agent integration.

The critical property is that loading a vault key never disturbs the host's own
keys, and that the vault key is always removed afterwards.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault.ssh_agent import (
    SshAgentError, add_key, agent_available, fingerprint_of, is_ram_backed,
    list_agent_fingerprints, remove_key, _ram_backed_dir,
)

HAVE_AGENT = agent_available() and shutil.which("ssh-keygen") is not None


def make_key(directory: str, name: str, passphrase: str = "") -> bytes:
    path = os.path.join(directory, name)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", passphrase, "-f", path, "-q", "-C", name],
        check=True, capture_output=True,
    )
    with open(path, "rb") as fh:
        return fh.read()


class TestStaging(unittest.TestCase):
    def test_staging_dir_is_ram_backed(self):
        """Private keys must not touch persistent storage."""
        directory = _ram_backed_dir()
        try:
            if os.path.isdir("/dev/shm"):
                self.assertTrue(is_ram_backed(directory),
                                f"{directory} is not RAM-backed")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_staging_dir_is_private(self):
        directory = _ram_backed_dir()
        try:
            mode = os.stat(directory).st_mode & 0o777
            self.assertEqual(mode, 0o700, "staging dir must be owner-only")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


@unittest.skipUnless(HAVE_AGENT, "needs a running ssh-agent")
class TestAgentIntegration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.added: list[bytes] = []

    def tearDown(self):
        for key in self.added:
            remove_key(key)
        self.dir.cleanup()

    def _add(self, key_bytes, **kwargs):
        info = add_key(key_bytes, **kwargs)
        self.added.append(key_bytes)
        return info

    def test_fingerprint_matches_ssh_keygen(self):
        key = make_key(self.dir.name, "k")
        result = subprocess.run(
            ["ssh-keygen", "-lf", os.path.join(self.dir.name, "k")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(fingerprint_of(key), result.stdout.split()[1])

    def test_add_then_remove(self):
        key = make_key(self.dir.name, "k")
        before = list_agent_fingerprints()

        info = self._add(key)
        self.assertIn(info["fingerprint"], list_agent_fingerprints())

        self.assertTrue(remove_key(key))
        self.added.remove(key)
        self.assertEqual(list_agent_fingerprints(), before)

    def test_host_keys_are_untouched(self):
        """The whole point: adding a vault key must not evict existing keys."""
        host = make_key(self.dir.name, "host")
        vault = make_key(self.dir.name, "vault")

        self._add(host)
        host_fp = fingerprint_of(host)
        self.assertIn(host_fp, list_agent_fingerprints())

        self._add(vault)
        loaded = list_agent_fingerprints()
        self.assertIn(host_fp, loaded, "host key was evicted")
        self.assertIn(fingerprint_of(vault), loaded)

        remove_key(vault)
        self.added.remove(vault)
        after = list_agent_fingerprints()
        self.assertIn(host_fp, after, "host key lost when removing the vault key")
        self.assertNotIn(fingerprint_of(vault), after)

    def test_encrypted_key_with_passphrase(self):
        key = make_key(self.dir.name, "enc", passphrase="secret123")
        info = self._add(key, passphrase="secret123")
        self.assertIn(info["fingerprint"], list_agent_fingerprints())

    def test_encrypted_key_wrong_passphrase_fails_clearly(self):
        key = make_key(self.dir.name, "enc2", passphrase="right")
        with self.assertRaises(SshAgentError) as ctx:
            add_key(key, passphrase="wrong")
        self.assertIn("passphrase", str(ctx.exception).lower())

    def test_encrypted_key_without_passphrase_does_not_hang(self):
        """A missing passphrase must fail fast, not block on a prompt."""
        key = make_key(self.dir.name, "enc3", passphrase="secret")
        with self.assertRaises(SshAgentError):
            add_key(key)

    def test_no_staging_files_left_behind(self):
        key = make_key(self.dir.name, "k2")
        self._add(key)
        leftovers = [d for d in os.listdir("/dev/shm") if d.startswith("amvault-")] \
            if os.path.isdir("/dev/shm") else []
        self.assertEqual(leftovers, [], f"staging dirs left behind: {leftovers}")

    def test_removing_a_key_not_in_agent_is_harmless(self):
        key = make_key(self.dir.name, "absent")
        self.assertFalse(remove_key(key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
