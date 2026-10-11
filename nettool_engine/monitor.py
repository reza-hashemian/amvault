"""Watch a chosen set of devices: is each one answering, and how is it doing.

Each watched device gets its own thread, so one that is slow or down never
delays the others.  Every tick it is pinged; if the vault gave us a way to
sign in and we know the platform, its CPU, memory and interface traffic are
read over SSH as well.

Credentials are fetched once, when watching starts, and held in memory only
for as long as the device is watched.  That is what lets monitoring carry on
after the vault auto-locks -- and why stopping, or closing the window, is what
ends it.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from . import net
from .collectors import COLLECTORS, finish
from .sshclient import Session, SshError

TICK_SECONDS = 5
# An hour of history per device stays in memory for the charts.
KEEP_SAMPLES = 3600 // TICK_SECONDS
# After a failed sign-in, wait this many ticks before trying again, so a wrong
# password does not hammer the device (or lock the account out).
RETRY_AFTER_TICKS = 12


class Watch:
    def __init__(self, device: dict, credentials: dict | None, note: str):
        self.device = device
        self.credentials = credentials
        self.note = note                 # why there are no metrics, if there are none
        self.samples: deque = deque(maxlen=KEEP_SAMPLES)
        self.stop = threading.Event()


class Monitor:
    def __init__(self, emit, history=None, session_factory=Session, ping=net.ping,
                 tick_seconds: float = TICK_SECONDS):
        self._emit = emit
        self._history = history
        self._session_factory = session_factory
        self._ping = ping
        self._tick = tick_seconds
        self._lock = threading.Lock()
        self._watches: dict[str, Watch] = {}

    def start(self, devices: list[dict], get_credentials) -> dict:
        """Watch exactly `devices`: new ones start, missing ones stop.

        `get_credentials(device_id)` returns the vault's fields for a device
        or raises; a device it fails for is still watched, by ping alone.
        """
        wanted = {device["id"]: device for device in devices}
        with self._lock:
            for device_id in list(self._watches):
                if device_id not in wanted:
                    self._watches.pop(device_id).stop.set()
            fresh = [d for d in wanted.values() if d["id"] not in self._watches]

        for device in fresh:
            credentials, note = None, ""
            if device.get("platform") not in COLLECTORS:
                note = "Ping only: reading CPU and traffic is not available for this kind of device yet."
            elif not device.get("address"):
                note = "This entry has no address."
            else:
                try:
                    credentials = get_credentials(device["id"])
                except Exception as exc:
                    note = f"Ping only: {exc}"
            watch = Watch(device, credentials, note)
            with self._lock:
                self._watches[device["id"]] = watch
            threading.Thread(target=self._run, args=(watch,), daemon=True).start()
        return self.state()

    def stop(self) -> dict:
        with self._lock:
            for watch in self._watches.values():
                watch.stop.set()
            self._watches.clear()
        return self.state()

    def state(self) -> dict:
        with self._lock:
            return {
                "tick": self._tick,
                "watching": [
                    {"device": w.device, "samples": list(w.samples)}
                    for w in self._watches.values()
                ],
            }

    # --- one device -----------------------------------------------------
    def _run(self, watch: Watch) -> None:
        device = watch.device
        collector = COLLECTORS.get(device.get("platform"))
        session = None
        previous, previous_at = None, 0.0
        wait_ticks = 0
        tick = 0
        try:
            while not watch.stop.is_set():
                started = time.monotonic()
                reach = self._ping(device["address"]) if device.get("address") else {"up": False, "ms": None}
                sample = {"t": time.time(), "up": reach["up"], "ms": reach["ms"],
                          "cpu": None, "mem_used": None, "mem_total": None, "uptime": None,
                          "rx_bps": None, "tx_bps": None, "ifaces": None, "note": watch.note}

                due = collector is not None and watch.credentials and tick % collector.every == 0
                if due and wait_ticks > 0:
                    wait_ticks -= 1
                elif due:
                    try:
                        if session is None:
                            session = self._open(device, watch.credentials)
                            previous = None
                        run = session.run_in_shell if collector.shell else session.run
                        raw = collector.parse([run(command) for command in collector.commands])
                        now = time.monotonic()
                        sample.update(finish(previous, raw, now - previous_at))
                        previous, previous_at = raw, now
                        watch.note = sample["note"] = ""
                    except SshError as exc:
                        if session is not None:
                            session.close()
                            session = None
                        watch.note = sample["note"] = str(exc)
                        wait_ticks = RETRY_AFTER_TICKS
                    except Exception as exc:
                        watch.note = sample["note"] = f"Could not read the device's answer: {exc}"
                        wait_ticks = RETRY_AFTER_TICKS

                if watch.stop.is_set():
                    break
                # The interface list is large and only the newest is shown, so
                # it rides on the live event but is not kept in the series.
                kept = dict(sample, ifaces=None)
                watch.samples.append(kept)
                if self._history is not None:
                    try:
                        self._history.add(device, kept)
                    except Exception:
                        pass
                self._emit({"event": "monitor.sample", "device": device["id"], "sample": sample})
                tick += 1
                watch.stop.wait(max(0.0, self._tick - (time.monotonic() - started)))
        finally:
            if session is not None:
                session.close()
            # Do not keep the password around once nobody is watching.
            watch.credentials = None

    def _open(self, device: dict, credentials: dict):
        fields = device.get("fields") or {}
        port = credentials.get("mgmt_port") or credentials.get("port") or fields.get("mgmt_port") or fields.get("port")
        return self._session_factory(
            device["address"],
            int(port) if str(port or "").isdigit() else 22,
            credentials.get("username") or "",
            password=credentials.get("password"),
            private_key=credentials.get("private_key"),
            passphrase=credentials.get("passphrase"),
        )
