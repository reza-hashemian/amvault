"""Tests for the vault core: kdbx round-trips, sync conflicts, and TOTP."""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pykeepass import PyKeePass

from vault import kdbx
from vault.kdbx import KdbxCredentialsError, KdbxError
from vault.manager import (
    SyncConflict, VaultLocked, VaultManager, estimate_strength,
    generate_passphrase, generate_password,
)
from vault.models import Attachment, Category, Entry
from vault import portable
from vault.totp import TotpError, generate as totp_generate, parse_uri


def make_keyfile(path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(os.urandom(64))
    return path


class TestTotp(unittest.TestCase):
    SEED = base64.b32encode(b"12345678901234567890").decode()

    def test_rfc6238_vectors(self):
        self.assertEqual(totp_generate(self.SEED, at=59), "287082")
        self.assertEqual(totp_generate(self.SEED, at=1111111109), "081804")
        self.assertEqual(totp_generate(self.SEED, at=1234567890), "005924")

    def test_sha256_vector(self):
        seed = base64.b32encode(b"12345678901234567890123456789012").decode()
        self.assertEqual(totp_generate(seed, at=59, algorithm="SHA256"), "119246")

    def test_accepts_spaced_lowercase_seed(self):
        messy = self.SEED.lower()
        messy = messy[:4] + " " + messy[4:8] + " " + messy[8:]
        self.assertEqual(totp_generate(messy, at=59), "287082")

    def test_rejects_invalid_seed(self):
        with self.assertRaises(TotpError):
            totp_generate("not-base32!!!")
        with self.assertRaises(TotpError):
            totp_generate("")

    def test_parse_uri(self):
        info = parse_uri(f"otpauth://totp/GitHub:me?secret={self.SEED}&issuer=GitHub&digits=8")
        self.assertEqual(info["secret"], self.SEED)
        self.assertEqual(info["digits"], 8)

    def test_rejects_hotp_uri(self):
        with self.assertRaises(TotpError):
            parse_uri(f"otpauth://hotp/x?secret={self.SEED}")

    def test_uri_roundtrip_preserves_seed(self):
        """The seed must survive the wrap/unwrap the kdbx layer does."""
        uri = kdbx._totp_uri(self.SEED, "My Account")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertEqual(kdbx._seed_from_uri(uri), self.SEED)

    def test_uri_wrapping_is_idempotent(self):
        uri = kdbx._totp_uri(self.SEED, "X")
        self.assertEqual(kdbx._totp_uri(uri, "X"), uri)

    def test_uri_label_sanitised(self):
        """Slashes and colons in a title would corrupt the URI path."""
        uri = kdbx._totp_uri(self.SEED, "Foo:Bar/Baz")
        self.assertNotIn(":Bar", uri)
        self.assertEqual(kdbx._seed_from_uri(uri), self.SEED)


class TestEntry(unittest.TestCase):
    def test_search_ignores_secret_values(self):
        e = Entry(title="Router", category=Category.NETWORK,
                  fields={"mgmt_ip": "192.168.1.1", "password": "s3cr3t-unique"})
        self.assertTrue(e.matches("router"))
        self.assertTrue(e.matches("192.168"))
        self.assertFalse(e.matches("s3cr3t-unique"),
                         "secret values must not be searchable")

    def test_search_matches_tags_and_attachments(self):
        e = Entry(title="Prod", tags=["work", "critical"])
        e.attachments.append(Attachment(name="id_ed25519", data=b"x"))
        self.assertTrue(e.matches("critical"))
        self.assertTrue(e.matches("ed25519"))


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.kdbx")

    def tearDown(self):
        self.dir.cleanup()

    def mgr(self):
        return VaultManager(self.path)


class TestVaultBasics(VaultTestCase):
    def test_create_unlock_roundtrip(self):
        m = self.mgr()
        m.create("master-pw")
        m.add(Entry(title="GitHub", fields={"username": "me", "password": "p"}))
        m.save()

        m2 = self.mgr()
        m2.unlock("master-pw")
        self.assertEqual(len(m2.entries), 1)
        self.assertEqual(m2.entries[0].fields["password"], "p")

    def test_wrong_password_rejected(self):
        self.mgr().create("right-pw")
        with self.assertRaises(KdbxCredentialsError):
            self.mgr().unlock("wrong-pw")

    def test_key_file_required(self):
        keyfile = make_keyfile(os.path.join(self.dir.name, "v.key"))
        m = self.mgr()
        m.create("pw", key_file_path=keyfile)
        m.add(Entry(title="X"))
        m.save()

        with self.assertRaises(KdbxCredentialsError):
            self.mgr().unlock("pw")

        m2 = self.mgr()
        m2.unlock("pw", key_file_path=keyfile)
        self.assertEqual(len(m2.entries), 1)

    def test_wrong_key_file_rejected(self):
        good = make_keyfile(os.path.join(self.dir.name, "good.key"))
        bad = make_keyfile(os.path.join(self.dir.name, "bad.key"))
        self.mgr().create("pw", key_file_path=good)
        with self.assertRaises(KdbxCredentialsError):
            self.mgr().unlock("pw", key_file_path=bad)

    def test_locked_vault_denies_access(self):
        m = self.mgr()
        m.create("pw")
        m.lock()
        self.assertTrue(m.is_locked)
        with self.assertRaises(VaultLocked):
            _ = m.entries

    def test_autolock_after_idle(self):
        m = VaultManager(self.path, autolock_seconds=1)
        m.create("pw")
        self.assertFalse(m.check_autolock())
        m._last_activity -= 2
        self.assertTrue(m.check_autolock())
        self.assertTrue(m.is_locked)

    def test_change_password(self):
        m = self.mgr()
        m.create("old-pw")
        m.add(Entry(title="Keep me"))
        m.save()
        m.change_password("new-pw")

        with self.assertRaises(KdbxCredentialsError):
            self.mgr().unlock("old-pw")
        m2 = self.mgr()
        m2.unlock("new-pw")
        self.assertEqual(m2.entries[0].title, "Keep me")

    def test_crud(self):
        m = self.mgr()
        m.create("pw")
        e = m.add(Entry(title="Original"))
        m.save()
        e = m.entries[0]
        e.title = "Renamed"
        m.update(e)
        m.save()
        self.assertEqual(self._titles("pw"), ["Renamed"])
        self.assertTrue(m.delete(e.id))
        m.save()
        self.assertEqual(self._titles("pw"), [])

    def _titles(self, pw):
        m = self.mgr()
        m.unlock(pw)
        return sorted(e.title for e in m.entries)

    def test_refuses_to_overwrite_existing_vault(self):
        self.mgr().create("pw")
        with self.assertRaises(KdbxError):
            self.mgr().create("other-pw")

    def test_backup_readable_with_same_password(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Backed up"))
        m.save()
        dest = m.backup(os.path.join(self.dir.name, "backups"))
        kp = PyKeePass(dest, password="pw")
        self.assertEqual(kp.entries[0].title, "Backed up")

    def test_save_leaves_no_temp_files(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="X"))
        m.save()
        leftovers = [f for f in os.listdir(self.dir.name) if f.startswith(".vault-")]
        self.assertEqual(leftovers, [])

    def test_no_plaintext_in_file(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="SuperSecretBank", fields={"password": "hunter2xyz"},
                    tags=["bank"]))
        m.save()
        with open(self.path, "rb") as fh:
            raw = fh.read()
        for needle in (b"SuperSecretBank", b"hunter2xyz", b"bank"):
            self.assertNotIn(needle, raw)


class TestKdbxMapping(VaultTestCase):
    """The mapping is what mobile clients actually read, so verify it directly."""

    def test_categories_become_groups(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Router", category=Category.NETWORK))
        m.add(Entry(title="Key", category=Category.SSH_KEY))
        m.save()

        kp = PyKeePass(self.path, password="pw")
        groups = {e.title: e.group.name for e in kp.entries}
        self.assertEqual(groups["Router"], kdbx.GROUP_NAMES[Category.NETWORK])
        self.assertEqual(groups["Key"], kdbx.GROUP_NAMES[Category.SSH_KEY])

    def test_category_survives_roundtrip(self):
        m = self.mgr()
        m.create("pw")
        for category in Category.ALL:
            m.add(Entry(title=f"E-{category}", category=category))
        m.save()

        m2 = self.mgr()
        m2.unlock("pw")
        got = {e.title: e.category for e in m2.entries}
        for category in Category.ALL:
            self.assertEqual(got[f"E-{category}"], category)

    def test_standard_fields_use_keepass_columns(self):
        """Other clients' autofill relies on these being the real fields."""
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Site", fields={
            "username": "u", "password": "p", "url": "https://x.test"}))
        m.save()

        kp = PyKeePass(self.path, password="pw")
        e = kp.entries[0]
        self.assertEqual(e.username, "u")
        self.assertEqual(e.password, "p")
        self.assertEqual(e.url, "https://x.test")
        self.assertNotIn("username", e.custom_properties or {})

    def test_secret_custom_fields_are_protected(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Switch", category=Category.NETWORK, fields={
            "mgmt_ip": "10.0.0.1", "enable_password": "secret"}))
        m.save()

        kp = PyKeePass(self.path, password="pw")
        e = kp.entries[0]
        self.assertTrue(e.is_custom_property_protected("enable_password"))
        self.assertFalse(e.is_custom_property_protected("mgmt_ip"))

    def test_totp_written_as_otpauth_uri(self):
        seed = base64.b32encode(b"12345678901234567890").decode()
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Gitlab", fields={"totp_secret": seed}))
        m.save()

        kp = PyKeePass(self.path, password="pw")
        self.assertTrue(kp.entries[0].otp.startswith("otpauth://totp/"))

        m2 = self.mgr()
        m2.unlock("pw")
        self.assertEqual(m2.entries[0].fields["totp_secret"], seed)

    def test_removing_totp_clears_otp_field(self):
        seed = base64.b32encode(b"12345678901234567890").decode()
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="X", fields={"totp_secret": seed}))
        m.save()

        e = m.entries[0]
        del e.fields["totp_secret"]
        m.update(e)
        m.save()

        kp = PyKeePass(self.path, password="pw")
        self.assertFalse(kp.entries[0].otp)

    def test_attachment_roundtrip(self):
        keydata = b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
        src = os.path.join(self.dir.name, "id_ed25519")
        with open(src, "wb") as fh:
            fh.write(keydata)

        m = self.mgr()
        m.create("pw")
        e = m.add(Entry(title="Server", category=Category.SSH_KEY))
        m.save()
        m.attach_file(m.entries[0].id, src)
        m.save()

        m2 = self.mgr()
        m2.unlock("pw")
        self.assertEqual(m2.entries[0].attachments[0].data, keydata)

        dest = os.path.join(self.dir.name, "out")
        m2.export_attachment(m2.entries[0].id, "id_ed25519", dest)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), keydata)
        if os.name == "posix":
            self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)

    def test_attachment_removal_persists(self):
        m = self.mgr()
        m.create("pw")
        src = os.path.join(self.dir.name, "f.txt")
        with open(src, "wb") as fh:
            fh.write(b"data")
        e = m.add(Entry(title="X"))
        m.save()
        m.attach_file(m.entries[0].id, src)
        m.save()
        m.remove_attachment(m.entries[0].id, "f.txt")
        m.save()

        m2 = self.mgr()
        m2.unlock("pw")
        self.assertEqual(m2.entries[0].attachments, [])

    def test_attachment_size_limit(self):
        big = os.path.join(self.dir.name, "big.bin")
        with open(big, "wb") as fh:
            fh.write(b"0" * (6 * 1024 * 1024))
        m = self.mgr()
        m.create("pw")
        e = m.add(Entry(title="X"))
        m.save()
        with self.assertRaises(ValueError):
            m.attach_file(m.entries[0].id, big)

    def test_changing_category_moves_group(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Thing", category=Category.LOGIN))
        m.save()

        e = m.entries[0]
        e.category = Category.NETWORK
        m.update(e)
        m.save()

        kp = PyKeePass(self.path, password="pw")
        self.assertEqual(kp.entries[0].group.name,
                         kdbx.GROUP_NAMES[Category.NETWORK])

    def test_field_deletion_persists(self):
        """A removed custom field must not linger in the file."""
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="X", category=Category.NETWORK,
                    fields={"mgmt_ip": "1.1.1.1", "vendor": "Cisco"}))
        m.save()

        e = m.entries[0]
        del e.fields["vendor"]
        m.update(e)
        m.save()

        m2 = self.mgr()
        m2.unlock("pw")
        self.assertNotIn("vendor", m2.entries[0].fields)

    def test_entry_written_by_another_client_is_readable(self):
        """Simulates an entry created on the phone."""
        m = self.mgr()
        m.create("pw")
        m.save()

        kp = PyKeePass(self.path, password="pw")
        group = kp.find_groups(name=kdbx.GROUP_NAMES[Category.LOGIN], first=True)
        kp.add_entry(group, "From Phone", "phoneuser", "phonepass")
        kp.save()

        m2 = self.mgr()
        m2.unlock("pw")
        titles = [e.title for e in m2.entries]
        self.assertIn("From Phone", titles)
        entry = next(e for e in m2.entries if e.title == "From Phone")
        self.assertEqual(entry.fields["username"], "phoneuser")
        self.assertEqual(entry.category, Category.LOGIN)


class TestSyncConflict(VaultTestCase):
    def test_save_refuses_when_file_changed(self):
        m = self.mgr()
        m.create("pw")
        m.add(Entry(title="Desktop edit"))
        m.save()

        # Another device writes the file while we hold it open.
        time.sleep(0.01)
        other = PyKeePass(self.path, password="pw")
        group = other.find_groups(name=kdbx.GROUP_NAMES[Category.LOGIN], first=True)
        other.add_entry(group, "Phone edit", "u", "p")
        other.save()

        m.add(Entry(title="Another desktop edit"))
        with self.assertRaises(SyncConflict) as ctx:
            m.save()
        # The other side's version must be preserved, not destroyed.
        self.assertIsNotNone(ctx.exception.backup_path)
        self.assertTrue(os.path.exists(ctx.exception.backup_path))

    def test_force_save_overwrites(self):
        m = self.mgr()
        m.create("pw")
        m.save()
        time.sleep(0.01)
        other = PyKeePass(self.path, password="pw")
        group = other.find_groups(name=kdbx.GROUP_NAMES[Category.LOGIN], first=True)
        other.add_entry(group, "Phone edit", "u", "p")
        other.save()

        m.add(Entry(title="Desktop wins"))
        m.save(force=True)

        m2 = self.mgr()
        m2.unlock("pw")
        self.assertEqual([e.title for e in m2.entries], ["Desktop wins"])

    def test_reload_adopts_disk_version(self):
        m = self.mgr()
        m.create("pw")
        m.save()
        time.sleep(0.01)
        other = PyKeePass(self.path, password="pw")
        group = other.find_groups(name=kdbx.GROUP_NAMES[Category.LOGIN], first=True)
        other.add_entry(group, "Phone edit", "u", "p")
        other.save()

        m.add(Entry(title="Discard me"))
        m.reload()
        self.assertEqual([e.title for e in m.entries], ["Phone edit"])
        self.assertFalse(m.dirty)

    def test_no_conflict_on_normal_save(self):
        m = self.mgr()
        m.create("pw")
        for i in range(3):
            m.add(Entry(title=f"E{i}"))
            m.save()  # must not raise
        self.assertEqual(len(self.mgr_entries("pw")), 3)

    def mgr_entries(self, pw):
        m = self.mgr()
        m.unlock(pw)
        return m.entries


class TestPasswordGeneration(unittest.TestCase):
    def test_length_and_classes(self):
        pw = generate_password(32)
        self.assertEqual(len(pw), 32)
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))

    def test_digits_only(self):
        pw = generate_password(10, use_upper=False, use_lower=False, use_symbols=False)
        self.assertTrue(pw.isdigit())

    def test_avoid_ambiguous(self):
        for _ in range(20):
            pw = generate_password(40, avoid_ambiguous=True)
            self.assertFalse(any(c in "Il1O0" for c in pw))

    def test_rejects_impossible_requests(self):
        with self.assertRaises(ValueError):
            generate_password(20, False, False, False, False)
        with self.assertRaises(ValueError):
            generate_password(2)

    def test_passphrase(self):
        self.assertEqual(len(generate_passphrase(words=5).split("-")), 5)

    def test_strength_ordering(self):
        self.assertLess(estimate_strength("abc")["bits"],
                        estimate_strength(generate_password(32))["bits"])
        self.assertEqual(estimate_strength("")["label"], "empty")


class TestPortableExport(VaultTestCase):
    """Export/import, i.e. moving into a new vault after a key leak."""

    KEYDATA = b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END-----\n"

    def populated_vault(self, password="oldpw"):
        m = self.mgr()
        m.create(password)
        m.add(Entry(
            title="GitHub", category=Category.LOGIN,
            fields={"username": "reza", "password": "hunter2",
                    "url": "https://github.com"},
            tags=["work"], notes="the main account",
        ))
        m.add(Entry(
            title="Prod server", category=Category.SSH_KEY,
            fields={"username": "root", "host": "10.0.0.5",
                    "passphrase": "keypass"},
        ))
        m.save()

        src = os.path.join(self.dir.name, "id_ed25519")
        with open(src, "wb") as fh:
            fh.write(self.KEYDATA)
        ssh_entry = next(e for e in m.entries if e.title == "Prod server")
        m.attach_file(ssh_entry.id, src)
        m.save()
        return m

    def test_export_includes_attachment_bytes(self):
        m = self.populated_vault()
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(m.entries, out)

        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["format"], portable.FORMAT)
        self.assertEqual(payload["entry_count"], 2)

        entry = next(e for e in payload["entries"] if e["title"] == "Prod server")
        blob = entry["attachments"][0]
        self.assertEqual(blob["name"], "id_ed25519")
        self.assertEqual(base64.b64decode(blob["data"]), self.KEYDATA)

    def test_export_file_is_owner_only(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        m = self.populated_vault()
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(m.entries, out)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)

    def test_rotation_roundtrip_into_new_vault(self):
        """The whole point: old vault out, new vault in, nothing lost."""
        old = self.populated_vault("oldpw")
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(old.entries, out)

        new_path = os.path.join(self.dir.name, "new.kdbx")
        keyfile = make_keyfile(os.path.join(self.dir.name, "new.key"))
        new = VaultManager(new_path)
        new.create("a-completely-different-password", keyfile)
        for entry in portable.read_export(out):
            new.add(entry)
        new.save()

        reopened = VaultManager(new_path)
        reopened.unlock("a-completely-different-password", keyfile)
        titles = sorted(e.title for e in reopened.entries)
        self.assertEqual(titles, ["GitHub", "Prod server"])

        github = next(e for e in reopened.entries if e.title == "GitHub")
        self.assertEqual(github.fields["password"], "hunter2")
        self.assertEqual(github.fields["username"], "reza")
        self.assertEqual(github.tags, ["work"])
        self.assertEqual(github.notes, "the main account")

        ssh = next(e for e in reopened.entries if e.title == "Prod server")
        self.assertEqual(ssh.category, Category.SSH_KEY)
        self.assertEqual(ssh.fields["passphrase"], "keypass")
        self.assertEqual(ssh.attachments[0].data, self.KEYDATA)

    def test_old_credentials_do_not_open_the_new_vault(self):
        """Rotation is pointless if the leaked password still works."""
        old = self.populated_vault("oldpw")
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(old.entries, out)

        new_path = os.path.join(self.dir.name, "new.kdbx")
        new = VaultManager(new_path)
        new.create("newpw")
        for entry in portable.read_export(out):
            new.add(entry)
        new.save()

        with self.assertRaises(KdbxCredentialsError):
            VaultManager(new_path).unlock("oldpw")

    def test_import_assigns_fresh_ids(self):
        """Reused KeePass uuids would overwrite entries instead of adding."""
        m = self.populated_vault()
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(m.entries, out)

        original_ids = {e.id for e in m.entries}
        imported_ids = {e.id for e in portable.read_export(out)}
        self.assertEqual(len(imported_ids), 2)
        self.assertFalse(original_ids & imported_ids)

    def test_importing_twice_duplicates_rather_than_merges(self):
        m = self.populated_vault()
        out = os.path.join(self.dir.name, "export.json")
        portable.write_export(m.entries, out)

        target = VaultManager(os.path.join(self.dir.name, "target.kdbx"))
        target.create("pw")
        for _ in range(2):
            for entry in portable.read_export(out):
                target.add(entry)
        target.save()
        self.assertEqual(len(target.entries), 4)

    def test_rejects_files_that_are_not_exports(self):
        cases = {
            "not json": "{{{",
            "wrong format": '{"format": "something-else", "version": 1}',
            "future version": '{"format": "amvault-export", "version": 99, "entries": []}',
            "no entries list": '{"format": "amvault-export", "version": 1}',
            "entry not an object": (
                '{"format": "amvault-export", "version": 1, "entries": [42]}'
            ),
            "bad base64 attachment": json.dumps({
                "format": "amvault-export", "version": 1,
                "entries": [{"title": "X", "attachments": [
                    {"name": "k", "data": "!!!not base64!!!"}]}],
            }),
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(portable.ImportError_):
                    portable.parse_export(text)

    def test_missing_file_is_reported_cleanly(self):
        with self.assertRaises(portable.ImportError_):
            portable.read_export(os.path.join(self.dir.name, "nope.json"))

    def test_summarise_counts_attachments(self):
        m = self.populated_vault()
        counts = portable.summarise(m.entries)
        self.assertEqual(counts["entries"], 2)
        self.assertEqual(counts["attachments"], 1)
        self.assertEqual(counts["attachment_bytes"], len(self.KEYDATA))


if __name__ == "__main__":
    unittest.main(verbosity=2)
