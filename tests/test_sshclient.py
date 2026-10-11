"""Tests for the SSH session against a real SSH server running in-process.

The server is paramiko's own, on a loopback port, so sign-in, both ways of
running commands, and host-key pinning are exercised over an actual SSH
connection rather than against mocks.
"""

import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import paramiko
except ImportError:  # the engine runs without it; these tests cannot
    paramiko = None

from nettool_engine import sshclient
from nettool_engine.sshclient import Session, SshError

PROMPT = "SW-TEST#"


class _Server(paramiko.ServerInterface if paramiko else object):
    def check_auth_password(self, username, password):
        ok = (username, password) == ("netadmin", "s3cret")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        channel.send(f"ran: {command.decode()}\n".encode())
        channel.send_exit_status(0)
        channel.shutdown_write()
        threading.Timer(0.05, channel.close).start()
        return True

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=_fake_ios, args=(channel,), daemon=True).start()
        return True


# Set by a test to make the fake device start in user mode, as an account
# without privilege 15 does.
USER_MODE = {"on": False, "secret": "en-s3cret", "typed": []}


def _fake_ios(channel):
    """Just enough of an IOS exec session: a banner, a prompt, echoed input,
    and `enable` asking for its secret."""
    prompt = "SW-TEST>" if USER_MODE["on"] else PROMPT
    asking = False
    try:
        channel.send(f"\r\nWelcome\r\n{prompt}".encode())
        buffer = b""
        while True:
            data = channel.recv(1024)
            if not data:
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                command = line.decode().strip()
                if asking:
                    # A real device does not echo what is typed here.
                    USER_MODE["typed"].append(command)
                    if command == USER_MODE["secret"]:
                        prompt, asking = PROMPT, False
                        channel.send(f"\r\n{prompt}".encode())
                    elif len(USER_MODE["typed"]) >= 3:
                        asking = False
                        channel.send(f"\r\n% Bad secrets\r\n\r\n{prompt}".encode())
                    else:
                        channel.send(b"\r\nPassword: ")
                elif command == "enable" and prompt.endswith(">"):
                    asking = True
                    channel.send(b"enable\r\nPassword: ")
                else:
                    reply = "" if command.startswith("terminal") else f"output of {command}\r\nsecond # line\r\n"
                    channel.send(f"{command}\r\n{reply}{prompt}".encode())
    except (OSError, EOFError):
        pass


class Listener:
    def __init__(self, key):
        self.key = key
        self.interface = _Server
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(self.key)
            try:
                transport.start_server(server=self.interface())
            except Exception:
                continue
            self.transports.append(transport)

    def close(self):
        # shutdown, not just close: a thread blocked in accept() otherwise
        # keeps the port open.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        for transport in self.transports:
            transport.close()


@unittest.skipIf(paramiko is None, "paramiko is not installed")
class TestSession(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        os.environ["NETTOOL_DATA_DIR"] = self.data.name
        self.addCleanup(os.environ.pop, "NETTOOL_DATA_DIR", None)
        # Keep the developer's own ~/.ssh/known_hosts out of the picture.
        self._real_load = paramiko.SSHClient.load_system_host_keys
        paramiko.SSHClient.load_system_host_keys = lambda self, filename=None: None
        self.addCleanup(setattr, paramiko.SSHClient, "load_system_host_keys", self._real_load)
        self.server = Listener(paramiko.RSAKey.generate(2048))
        self.addCleanup(lambda: self.server.close())

    def connect(self, password="s3cret", username="netadmin", port=None):
        session = Session("127.0.0.1", port or self.server.port, username, password=password)
        self.addCleanup(session.close)
        return session

    def test_exec_runs_one_command_after_another(self):
        session = self.connect()
        self.assertEqual(session.run("uptime"), "ran: uptime\n")
        self.assertEqual(session.run("cat /proc/stat"), "ran: cat /proc/stat\n")

    def test_shell_returns_output_without_echo_or_prompt(self):
        session = self.connect()
        self.assertEqual(session.run_in_shell("show version"),
                         "output of show version\nsecond # line")
        # The same session keeps working for the next command.
        self.assertEqual(session.run_in_shell("show interfaces"),
                         "output of show interfaces\nsecond # line")

    def test_enable_enters_privileged_mode_with_the_secret(self):
        USER_MODE.update(on=True, typed=[])
        self.addCleanup(USER_MODE.update, on=False)
        session = self.connect()
        self.assertEqual(session.shell_prompt(), "SW-TEST>")
        self.assertTrue(session.enable("en-s3cret"))
        self.assertEqual(session.prompt, PROMPT)
        self.assertEqual(session.run_in_shell("show version"), "output of show version\nsecond # line")

    def test_enable_with_a_wrong_secret_backs_out_and_stays_usable(self):
        USER_MODE.update(on=True, typed=[])
        self.addCleanup(USER_MODE.update, on=False)
        session = self.connect()
        self.assertFalse(session.enable("wrong"))
        self.assertEqual(session.prompt, "SW-TEST>")
        # The session is back at a prompt, not stuck at "Password:".
        self.assertEqual(session.run_in_shell("show version"), "output of show version\nsecond # line")

    def test_enable_without_a_secret_sends_nothing(self):
        USER_MODE.update(on=True, typed=[])
        self.addCleanup(USER_MODE.update, on=False)
        session = self.connect()
        self.assertFalse(session.enable(None))
        self.assertEqual(USER_MODE["typed"], [])

    def test_already_privileged_needs_no_secret(self):
        self.assertTrue(self.connect().enable(None))

    def test_wrong_password(self):
        with self.assertRaises(SshError) as caught:
            self.connect(password="nope")
        self.assertIn("refused", str(caught.exception))

    def test_missing_username_or_secret_never_connects(self):
        with self.assertRaises(SshError):
            Session("127.0.0.1", self.server.port, "", password="x")
        with self.assertRaises(SshError):
            Session("127.0.0.1", self.server.port, "netadmin")

    def test_nothing_listening(self):
        self.server.close()
        with self.assertRaises(SshError) as caught:
            self.connect()
        self.assertIn("Could not connect", str(caught.exception))

    def test_host_key_is_pinned_on_first_use(self):
        self.connect()
        with open(sshclient.known_hosts_path(), encoding="utf-8") as fh:
            self.assertIn(f"[127.0.0.1]:{self.server.port}", fh.read())
        self.connect()        # the same key again is fine

    def test_changed_host_key_is_refused_before_the_password_is_sent(self):
        self.connect()

        attempts = []

        class Recording(_Server):
            def check_auth_password(self, username, password):
                attempts.append(password)
                return paramiko.AUTH_FAILED

        # Same address, different key: what a replaced device -- or someone
        # in the middle -- looks like.
        self.server.key = paramiko.RSAKey.generate(2048)
        self.server.interface = Recording
        with self.assertRaises(SshError) as caught:
            self.connect()
        self.assertIn("host key", str(caught.exception))
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
