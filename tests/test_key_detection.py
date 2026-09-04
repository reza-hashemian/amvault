"""Tests for picking the private key out of an entry's attachments.

Detection reads file headers rather than filenames, because a key saved as
`github_backup` is still a private key and one named `id_rsa.pub` is still
public.  These cases pin that down.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault.models import Attachment, Category, Entry


def load_cli():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "amvault_cli", os.path.join(here, "amvault.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()

# Detection itself lives in the shared module; the CLI re-exports it.
import amvault_keys

RSA_HEADER = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n"
PKCS8 = b"-----BEGIN PRIVATE KEY-----\nMIIEvg...\n"
ENCRYPTED = b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIFHz...\n"


def entry_with(*attachments):
    e = Entry(title="Test", category=Category.SSH_KEY)
    for name, data in attachments:
        e.attachments.append(Attachment(name=name, data=data))
    return e


class TestContentDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        path = os.path.join(cls.dir.name, "k")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", path, "-q"],
            check=True, capture_output=True,
        )
        with open(path, "rb") as fh:
            cls.private = fh.read()
        with open(path + ".pub", "rb") as fh:
            cls.public = fh.read()

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_recognises_openssh_private_key(self):
        self.assertTrue(amvault_keys.looks_like_private_key(self.private))

    def test_recognises_legacy_private_formats(self):
        for blob in (RSA_HEADER, PKCS8, ENCRYPTED):
            self.assertTrue(amvault_keys.looks_like_private_key(blob), blob[:30])

    def test_public_key_is_not_private(self):
        self.assertFalse(amvault_keys.looks_like_private_key(self.public))
        self.assertTrue(amvault_keys.looks_like_public_key(self.public))

    def test_plain_text_is_neither(self):
        self.assertFalse(amvault_keys.looks_like_private_key(b"just some notes\n"))
        self.assertFalse(amvault_keys.looks_like_public_key(b"just some notes\n"))

    def test_leading_whitespace_tolerated(self):
        self.assertTrue(amvault_keys.looks_like_private_key(b"\n\n  " + self.private))


class TestPicking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TestContentDetection.setUpClass()
        cls.private = TestContentDetection.private
        cls.public = TestContentDetection.public

    @classmethod
    def tearDownClass(cls):
        TestContentDetection.tearDownClass()

    def test_single_key(self):
        e = entry_with(("id_ed25519", self.private))
        self.assertEqual(cli._pick_key_attachment(e, None).name, "id_ed25519")

    def test_skips_public_half(self):
        e = entry_with(("id_ed25519", self.private), ("id_ed25519.pub", self.public))
        self.assertEqual(cli._pick_key_attachment(e, None).name, "id_ed25519")

    def test_skips_public_key_named_without_pub_suffix(self):
        """Filename gives no hint here; only the content does."""
        e = entry_with(("id_ed25519", self.private), ("mykey_public", self.public))
        self.assertEqual(cli._pick_key_attachment(e, None).name, "id_ed25519")

    def test_skips_unrelated_files(self):
        e = entry_with(("id_ed25519", self.private), ("notes.txt", b"hello\n"))
        self.assertEqual(cli._pick_key_attachment(e, None).name, "id_ed25519")

    def test_private_key_with_arbitrary_name(self):
        e = entry_with(("github_backup", self.private))
        self.assertEqual(cli._pick_key_attachment(e, None).name, "github_backup")

    def test_public_only_is_rejected(self):
        e = entry_with(("id_ed25519.pub", self.public))
        self.assertIsNone(cli._pick_key_attachment(e, None))

    def test_no_attachments_is_rejected(self):
        self.assertIsNone(cli._pick_key_attachment(entry_with(), None))

    def test_two_private_keys_require_a_choice(self):
        e = entry_with(("a", self.private), ("b", RSA_HEADER))
        self.assertIsNone(cli._pick_key_attachment(e, None))

    def test_explicit_name_selects_it(self):
        e = entry_with(("a", self.private), ("b", RSA_HEADER))
        self.assertEqual(cli._pick_key_attachment(e, "b").name, "b")

    def test_explicit_name_rejects_a_public_key(self):
        e = entry_with(("id_ed25519", self.private), ("id_ed25519.pub", self.public))
        self.assertIsNone(cli._pick_key_attachment(e, "id_ed25519.pub"))

    def test_explicit_missing_name_is_rejected(self):
        e = entry_with(("id_ed25519", self.private))
        self.assertIsNone(cli._pick_key_attachment(e, "nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
