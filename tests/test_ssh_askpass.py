"""Tests for signing in to ssh with the vault's password.

The helper is run as ssh runs it: as a separate program, with the question as
its argument and the answer read from its stdout.
"""

import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault import ssh_connect
from vault.models import Category, Entry
from vault.ssh_askpass import HELPER, PasswordOffer


def ask(environment, prompt, stdin=""):
    """Run the launcher the way ssh would; returns (exit code, stdout)."""
    done = subprocess.run(
        [environment["SSH_ASKPASS"], prompt], input=stdin, capture_output=True, text=True,
        env={**os.environ, **environment}, timeout=20, start_new_session=True,
    )
    return done.returncode, done.stdout


class TestPasswordOffer(unittest.TestCase):
    def offer(self, password="hunter2", **kwargs):
        offer = PasswordOffer(password, **kwargs)
        self.addCleanup(offer.close)
        return offer

    def test_the_password_question_is_answered_from_the_vault(self):
        offer = self.offer("p@ss w0rd'\"$")
        code, out = ask(offer.environment(), "admin@10.0.0.1's password: ")
        self.assertEqual((code, out), (0, "p@ss w0rd'\"$\n"))
        self.assertTrue(offer.collected)

    def test_it_is_handed_over_only_once(self):
        offer = self.offer()
        environment = offer.environment()
        self.assertEqual(ask(environment, "Password: ")[1], "hunter2\n")
        # ssh asking again means the server refused it; the helper must not
        # produce it a second time (there is no terminal here, so it fails).
        code, out = ask(environment, "Password: ")
        self.assertNotIn("hunter2", out)
        self.assertNotEqual(code, 0)

    def test_host_key_and_passphrase_questions_never_get_the_password(self):
        offer = self.offer()
        for prompt in ("Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
                       "Enter passphrase for key '/home/u/.ssh/id_ed25519': "):
            code, out = ask(offer.environment(), prompt)
            self.assertNotIn("hunter2", out, prompt)
        self.assertFalse(offer.collected)

    @unittest.skipIf(os.name == "nt", "needs a pseudo-terminal")
    def test_a_host_key_question_is_put_to_the_person_at_the_terminal(self):
        """Run with a real controlling terminal, as under ssh: the question
        must appear there and the typed answer must come back on stdout."""
        import pty
        import select

        offer = self.offer()
        environment = {**os.environ, **offer.environment()}
        prompt = "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        read_end, write_end = os.pipe()
        pid, terminal = pty.fork()
        if pid == 0:                                   # the helper, as ssh's child
            os.dup2(write_end, 1)
            os.execve(environment["SSH_ASKPASS"], [environment["SSH_ASKPASS"], prompt], environment)
        os.close(write_end)
        shown = b""
        while b"fingerprint])? " not in shown:
            self.assertTrue(select.select([terminal], [], [], 10)[0], "the question never appeared")
            shown += os.read(terminal, 1024)
        os.write(terminal, b"yes\n")
        self.assertTrue(select.select([read_end], [], [], 10)[0])
        answer = os.read(read_end, 1024)
        _, status = os.waitpid(pid, 0)
        os.close(read_end)
        os.close(terminal)
        self.assertEqual(answer, b"yes\n")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertFalse(offer.collected)              # and the password stayed put

    def test_nothing_secret_is_in_the_environment_or_on_disk(self):
        offer = self.offer()
        environment = offer.environment()
        self.assertNotIn("hunter2", repr(environment))
        directory = os.path.dirname(environment["SSH_ASKPASS"])
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    self.assertNotIn(b"hunter2", fh.read())
        if os.name != "nt":
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_a_wrong_token_gets_nothing(self):
        environment = dict(self.offer().environment(), AMVAULT_ASKPASS_TOKEN="00" * 32)
        code, out = ask(environment, "Password: ")
        self.assertNotIn("hunter2", out)

    def test_an_uncollected_offer_expires(self):
        offer = self.offer(lifetime=0.3)
        time.sleep(1.0)
        code, out = ask(offer.environment(), "Password: ")
        self.assertNotIn("hunter2", out)
        self.assertFalse(offer.collected)

    def test_the_helper_is_where_the_launcher_points(self):
        self.assertTrue(os.path.isfile(HELPER))


class TestInstalledCopy(unittest.TestCase):
    """An installed amvault has no separate Python, so it is its own helper."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_from_source_the_launcher_runs_the_helper_script(self):
        from vault import ssh_askpass
        self.assertEqual(ssh_askpass.helper_command(), f'"{sys.executable}" "{HELPER}"')

    def test_when_frozen_the_launcher_runs_the_app_itself(self):
        from unittest import mock
        from vault import ssh_askpass
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/opt/amvault/amvault"):
            self.assertEqual(ssh_askpass.helper_command(), '"/opt/amvault/amvault" --askpass')

    def test_the_app_answers_as_a_helper_without_opening_a_window(self):
        offer = PasswordOffer("hunter2")
        self.addCleanup(offer.close)
        environment = {**os.environ, **offer.environment()}
        environment.pop("DISPLAY", None)          # proves no window is involved
        environment.pop("WAYLAND_DISPLAY", None)
        done = subprocess.run(
            [sys.executable, os.path.join(self.ROOT, "amvault-gui.py"), "--askpass", "admin@host's password: "],
            capture_output=True, text=True, env=environment, timeout=30, start_new_session=True,
        )
        self.assertEqual((done.returncode, done.stdout), (0, "hunter2\n"))

    def test_self_check_imports_everything_and_exits_cleanly(self):
        done = subprocess.run(
            [sys.executable, os.path.join(self.ROOT, "amvault-gui.py"), "--self-check"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)


class TestConnectOffersThePassword(unittest.TestCase):
    def test_variables_ride_on_the_ssh_command_line(self):
        wrapped = ssh_connect.wrap_in_shell(["ssh", "admin@10.0.0.1"], {"A": "x y", "B": "1"})
        if len(wrapped) == 3:      # ran through the login shell
            self.assertIn("A='x y' B=1 ssh admin@10.0.0.1\n", wrapped[2])
        else:
            self.assertEqual(wrapped[:3], ["env", "A=x y", "B=1"])

    def test_connect_passes_an_offer_only_when_there_is_a_password(self):
        from unittest import mock

        seen = []

        def fake_launch(command, title="amvault", env=None):
            seen.append(env)
            return mock.Mock(pid=1)

        entry = Entry(title="r", category=Category.NETWORK,
                      fields={"mgmt_ip": "10.0.0.1", "username": "admin"})
        with mock.patch.object(ssh_connect, "ssh_available", return_value=True), \
             mock.patch.object(ssh_connect, "launch_terminal", fake_launch):
            without = ssh_connect.connect(entry)
            with_password = ssh_connect.connect(entry, password="hunter2")
        self.assertIsNone(seen[0])
        self.assertFalse(without["password_supplied"])
        self.assertEqual(seen[1]["SSH_ASKPASS_REQUIRE"], "force")
        self.assertTrue(with_password["password_supplied"])
        self.assertNotIn("hunter2", repr(with_password) + repr(seen))


if __name__ == "__main__":
    unittest.main()
