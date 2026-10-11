"""Hand device credentials to the network tool without a second login.

The network tool is a separate program.  It should not ask for the master
password again, and it should not keep its own copy of anything secret -- two
stores means two things to keep in step and two places to leak from.  So the
unlocked vault stays here, and the tool asks for what it needs when it needs
it, the way ssh asks ssh-agent.

One session is one launch of the tool:

  * a fresh local endpoint (a Unix socket in a private directory; a named pipe
    on Windows) and a fresh random token are created for it;
  * the token reaches the tool on its stdin, never in argv or the environment,
    both of which other processes can read;
  * the endpoint accepts exactly one authenticated connection and is then
    closed, so the token is worthless the moment it has been used.

What a session may ask for is deliberately narrow.  Only entries that describe
a device are visible at all, their listing carries no secrets, and a secret is
released one entry at a time.  A bug or compromise in the tool therefore cannot
walk the rest of the vault.

The one thing a session may write is a new device entry for a host its scan
found -- a name and an address, never a secret, and never a change to an entry
that already exists.

When the vault locks, the session stays connected but every request is answered
with `locked` until it is unlocked again.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener

from .models import Category, Entry

IS_WINDOWS = sys.platform == "win32"

# How long a launched tool has to connect before the endpoint is withdrawn.
CONNECT_TIMEOUT_SECONDS = 30
# Requests are small JSON objects; anything larger is not one of ours.
MAX_MESSAGE_BYTES = 1024 * 1024
# Readers wake this often to notice they have been asked to stop.  Closing a
# connection from another thread does not interrupt a blocked read, so each
# reader polls and closes its own end instead.
POLL_SECONDS = 0.2

# Fields a device listing may carry.  An allowlist rather than "everything not
# marked secret": users add their own fields, and a custom `backup_password`
# is not in SECRET_FIELDS but must not leave the vault unasked.
LISTED_FIELDS = (
    "device_type", "vendor", "model", "mgmt_ip", "mgmt_port", "host", "port",
    "username", "wifi_ssid", "location", "firmware", "key_type", "fingerprint",
    # How to reach a machine that has no fixed address. An ID, not a secret:
    # connecting still needs the machine's own RustDesk password.
    "rustdesk_id",
)
# Fields released by `get_credentials`, on top of the entry's secret fields.
CREDENTIAL_FIELDS = ("username", "mgmt_ip", "mgmt_port", "host", "port")
# Fields `add_device` accepts.  Nothing secret, and no username: who signs in
# to a device is for the person to say, in amvault.
ADDABLE_FIELDS = ("device_type", "vendor", "model", "mgmt_ip", "mgmt_port", "location")
MAX_TITLE_LENGTH = 120
MAX_FIELD_LENGTH = 200


class BrokerError(Exception):
    """A request that cannot be served; the message is shown to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_device(entry: Entry) -> bool:
    """Whether the network tool may see this entry at all.

    Network devices always; SSH entries only when they name a host, since a
    bare key with no address is not something to connect to.
    """
    if entry.category == Category.NETWORK:
        return True
    if entry.category == Category.SSH_KEY:
        return bool((entry.fields.get("host") or "").strip())
    return False


def describe_device(entry: Entry) -> dict:
    """The non-secret view of a device: enough to list and identify it."""
    fields = entry.fields
    secret_names = entry.secret_field_names()
    return {
        "id": entry.id,
        "title": entry.title,
        "category": entry.category,
        "tags": list(entry.tags),
        "fields": {
            name: fields[name]
            for name in LISTED_FIELDS
            if fields.get(name) and name not in secret_names
        },
        "address": (fields.get("mgmt_ip") or fields.get("host") or "").strip(),
        # Whether a secret exists, never what it is -- the tool uses these to
        # say "no password stored" before trying to connect.
        "has_password": bool(fields.get("password")),
        "has_key": bool(entry.attachments),
    }


def _find_device(manager, request: dict) -> Entry:
    # `device`, not `id`: on the wire `id` is the request number.
    entry = manager.get(str(request.get("device") or ""))
    if entry is None or not is_device(entry):
        # The same answer for "no such entry" and "not a device", so the tool
        # cannot probe which other entry ids exist.
        raise BrokerError("not_found", "No such device in the vault.")
    return entry


def _private_key_text(entry: Entry) -> str | None:
    """The entry's SSH private key, if it carries one as an attachment.

    Chosen by content, the same way the connect button chooses, so a public
    half or an unrelated file is never handed over as the key.
    """
    from amvault_keys import pick_private_key

    attachment = pick_private_key(entry)
    if attachment is None:
        return None
    return attachment.data.decode("utf-8", "replace")


def _open_ssh(entry: Entry) -> dict:
    # Imported here: amvault_keys lives beside the entry points, and the vault
    # package must stay importable without it for the CLI's own tests.
    from amvault_keys import pick_private_key

    from .ssh_connect import SshConnectError, connect

    attachment = pick_private_key(entry)
    try:
        result = connect(
            entry,
            key_bytes=attachment.data if attachment is not None else None,
            passphrase=entry.fields.get("passphrase") or None,
            password=entry.fields.get("password") or None,
        )
    except SshConnectError as exc:
        raise BrokerError("ssh_failed", str(exc)) from exc
    return {
        "display": result["display"],
        "key_loaded": result["key_loaded"],
        "via_agent": result["via_agent"],
    }


def _add_device(manager, request: dict, on_change) -> dict:
    fields = request.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    clean = {
        name: str(fields[name]).strip()[:MAX_FIELD_LENGTH]
        for name in ADDABLE_FIELDS
        if str(fields.get(name) or "").strip()
    }
    try:
        address = str(ipaddress.ip_address(clean.get("mgmt_ip", "")))
    except ValueError:
        raise BrokerError("bad_request", "A new device needs a valid IP address.") from None
    clean["mgmt_ip"] = address

    for existing in manager.entries:
        if is_device(existing) and address in (
            (existing.fields.get("mgmt_ip") or "").strip(),
            (existing.fields.get("host") or "").strip(),
        ):
            raise BrokerError("exists", f"{address} is already in the vault as {existing.title!r}.")

    title = str(request.get("title") or "").strip()[:MAX_TITLE_LENGTH] or address
    entry = Entry(title=title, category=Category.NETWORK, fields=clean)
    manager.add(entry)
    manager.touch()
    if on_change is not None:
        on_change()
    else:
        manager.save()
    return {"device": describe_device(entry)}


def handle_request(manager, request: dict, on_change=None) -> dict:
    """Serve one request against the vault.  Raises BrokerError to refuse.

    `manager` is None while no vault is open (the unlock screen is showing).
    `on_change` is called after a request has added an entry, so a GUI can save
    through its own path (which knows how to resolve a sync conflict) and
    redraw; without it the vault is saved directly.
    """
    op = request.get("op")
    locked = manager is None or manager.is_locked

    if op == "status":
        return {
            "locked": locked,
            "vault": os.path.basename(manager.path) if manager is not None else None,
        }

    if locked:
        raise BrokerError("locked", "The vault is locked. Unlock amvault to continue.")

    if op == "list_devices":
        devices = [describe_device(e) for e in manager.entries if is_device(e)]
        devices.sort(key=lambda d: d["title"].lower())
        return {"devices": devices}

    if op == "get_credentials":
        entry = _find_device(manager, request)
        # Someone is actively working with this device; do not auto-lock under
        # them.  Listing does not count -- a background refresh would otherwise
        # keep the vault open for ever.
        manager.touch()
        names = set(CREDENTIAL_FIELDS) | entry.secret_field_names()
        fields = {n: entry.fields[n] for n in names if entry.fields.get(n)}
        key = _private_key_text(entry)
        if key:
            fields["private_key"] = key
        return {"id": entry.id, "fields": fields}

    if op == "ssh":
        entry = _find_device(manager, request)
        manager.touch()
        return _open_ssh(entry)

    if op == "add_device":
        return _add_device(manager, request, on_change)

    raise BrokerError("unknown_op", f"Unknown request: {op!r}")


def _reply(handler, request: dict) -> dict:
    """Run a request and wrap the outcome in the wire format."""
    try:
        return {"ok": True, "result": handler(request)}
    except BrokerError as exc:
        return {"ok": False, "code": exc.code, "error": str(exc)}
    except Exception as exc:  # never let a bug take the session down
        return {"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {exc}"}


class BrokerSession:
    """One launch of the network tool: a single authenticated connection.

    `handler` is called with each request dict and returns the result dict (or
    raises BrokerError).  It is called on the session's own thread, so a GUI
    must pass a handler that hops to its main thread.
    """

    def __init__(self, handler, on_closed=None):
        self._handler = handler
        self._on_closed = on_closed
        self._token = secrets.token_bytes(32)
        self._connection = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._expired = False

        if IS_WINDOWS:
            self._directory = None
            self.family = "AF_PIPE"
            self.address = rf"\\.\pipe\amvault-{secrets.token_hex(16)}"
        else:
            # mkdtemp gives a 0700 directory, which is what actually keeps
            # other users away from the socket inside it.
            base = os.environ.get("XDG_RUNTIME_DIR") or None
            if base and not os.path.isdir(base):
                base = None
            self._directory = tempfile.mkdtemp(prefix="amvault-broker-", dir=base)
            self.family = "AF_UNIX"
            self.address = os.path.join(self._directory, "s")

        self._listener = Listener(self.address, family=self.family, authkey=self._token)
        threading.Thread(target=self._serve, daemon=True).start()
        timer = threading.Timer(CONNECT_TIMEOUT_SECONDS, self._expire)
        timer.daemon = True
        timer.start()

    def handshake(self) -> dict:
        """What the tool needs in order to connect.  Contains the token."""
        return {
            "address": self.address,
            "family": self.family,
            "token": self._token.hex(),
        }

    def handshake_args(self) -> dict:
        """The same, shaped as BrokerClient's keyword arguments."""
        return {"address": self.address, "family": self.family,
                "token_hex": self._token.hex()}

    @property
    def is_alive(self) -> bool:
        return not self._closed.is_set()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    def notify(self, event: str, **data) -> None:
        """Tell the tool something happened (the vault locked, say)."""
        self._send({"event": event, **data})

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._withdraw_endpoint()
        # The connection itself is closed by _serve, which owns it.
        if self._on_closed is not None:
            self._on_closed(self)

    # --- internals ------------------------------------------------------
    def _expire(self) -> None:
        if self._connected.is_set() or self._closed.is_set():
            return
        self._expired = True
        # Closing a listener does not wake a thread already blocked in
        # accept(), so knock on the door ourselves; _serve sees the flag and
        # hangs up instead of serving.
        try:
            Client(self.address, family=self.family, authkey=self._token).close()
        except Exception:
            pass
        self.close()

    def _withdraw_endpoint(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass
        if self._directory:
            shutil.rmtree(self._directory, ignore_errors=True)

    def _send(self, message: dict) -> None:
        connection = self._connection
        if connection is None or self._closed.is_set():
            return
        data = json.dumps(message).encode("utf-8")
        with self._send_lock:
            try:
                connection.send_bytes(data)
            except (OSError, ValueError):
                pass

    def _serve(self) -> None:
        try:
            # accept() runs the challenge-response on the token and raises if
            # the peer does not know it.
            connection = self._listener.accept()
        except Exception:
            self.close()
            return
        if self._expired or self._closed.is_set():
            connection.close()
            return
        self._connection = connection
        self._connected.set()
        # One connection is all this token was ever for.
        self._withdraw_endpoint()

        connection = self._connection
        try:
            while not self._closed.is_set():
                if not connection.poll(POLL_SECONDS):
                    continue
                raw = connection.recv_bytes(MAX_MESSAGE_BYTES)
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                except ValueError:
                    self._send({"ok": False, "code": "bad_request", "error": "Malformed request."})
                    continue
                reply = _reply(self._handler, request)
                reply["id"] = request.get("id")
                self._send(reply)
        except (EOFError, OSError):
            pass  # the tool went away, which is how sessions normally end
        finally:
            self.close()
            try:
                connection.close()
            except OSError:
                pass


class BrokerClient:
    """The tool's end of a session.

    The reference implementation of the wire format, used by the tests.  The
    network tool carries its own copy rather than importing this one, so it
    never needs amvault's source on its path.
    """

    def __init__(self, address: str, family: str, token_hex: str, on_event=None):
        self._connection = Client(address, family=family, authkey=bytes.fromhex(token_hex))
        self._on_event = on_event
        self._lock = threading.Lock()
        self._waiting: dict[int, tuple[threading.Event, list]] = {}
        self._next_id = 0
        self._closing = False
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def request(self, op: str, timeout: float = 20.0, **params) -> dict:
        if self.closed.is_set():
            raise BrokerError("disconnected", "amvault is no longer running.")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            done, box = threading.Event(), []
            self._waiting[request_id] = (done, box)
            try:
                self._connection.send_bytes(
                    json.dumps({"id": request_id, "op": op, **params}).encode("utf-8")
                )
            except (OSError, ValueError) as exc:
                self._waiting.pop(request_id, None)
                raise BrokerError("disconnected", "amvault is no longer running.") from exc

        if not done.wait(timeout):
            with self._lock:
                self._waiting.pop(request_id, None)
            raise BrokerError("timeout", "amvault did not answer in time.")
        reply = box[0]
        if reply is None:
            raise BrokerError("disconnected", "amvault is no longer running.")
        if not reply.get("ok"):
            raise BrokerError(reply.get("code") or "error", reply.get("error") or "Request failed.")
        return reply.get("result") or {}

    def close(self) -> None:
        self._closing = True

    def _read(self) -> None:
        try:
            while not self._closing:
                if not self._connection.poll(POLL_SECONDS):
                    continue
                message = json.loads(self._connection.recv_bytes(MAX_MESSAGE_BYTES))
                if "event" in message:
                    if self._on_event is not None:
                        self._on_event(message)
                    continue
                with self._lock:
                    waiter = self._waiting.pop(message.get("id"), None)
                if waiter is not None:
                    waiter[1].append(message)
                    waiter[0].set()
        except (EOFError, OSError, ValueError, TypeError):
            pass
        finally:
            self.closed.set()
            try:
                self._connection.close()
            except OSError:
                pass
            with self._lock:
                waiting, self._waiting = self._waiting, {}
            for done, box in waiting.values():
                box.append(None)
                done.set()
            if self._on_event is not None:
                self._on_event({"event": "closed"})
