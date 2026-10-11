"""Talk to amvault's credential broker.

The tool never stores a password.  It is started by amvault, which hands it a
one-time token; with that it opens a single connection back and asks for what
it needs, when it needs it.  Nothing here touches disk.

The wire format is JSON over a `multiprocessing.connection`, whose handshake
proves both ends know the token:

    -> {"id": 7, "op": "list_devices"}
    <- {"id": 7, "ok": true, "result": {...}}
    <- {"id": 7, "ok": false, "code": "locked", "error": "..."}
    <- {"event": "locked"}                      (unprompted, no id)

This mirrors `vault/broker.py` in amvault.  It is a copy rather than an import
so the tool never needs amvault's source on its path.
"""

from __future__ import annotations

import json
import threading
from multiprocessing.connection import Client

MAX_MESSAGE_BYTES = 1024 * 1024
# The reader wakes this often to notice it has been asked to stop; closing a
# connection from another thread does not interrupt a blocked read.
POLL_SECONDS = 0.2


class BrokerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrokerClient:
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
