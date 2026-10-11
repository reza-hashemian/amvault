"""Engine entry point: JSON lines in on stdin, JSON lines out on stdout.

    <- first line: the handshake from the shell ({} when run by hand)
    <- {"id": 1, "method": "devices.list", "params": {}}
    -> {"id": 1, "result": {...}}   or   {"id": 1, "error": "..."}
    -> {"event": "vault.locked"}    (unprompted)

stdout carries the protocol and nothing else; anything meant for a human goes
to stderr.  The process exits when stdin closes, so it can never outlive the
window that started it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import demo, net, platforms, remote
from .monitor import Monitor
from .panels import PanelError, open_panel
from .sshclient import SshError
from .broker_client import BrokerClient, BrokerError


class EngineError(Exception):
    """A request that cannot be served; the message is shown in the window."""


class Engine:
    def __init__(self, handshake: dict, emit):
        self._emit = emit
        self._broker: BrokerClient | None = None
        self._demo = os.environ.get("NETTOOL_DEMO") == "1"
        self._was_connected = False
        self._devices: list[dict] = []
        self._identified: dict[tuple, str] = {}
        self._scanner = net.Scanner(emit)
        self._pings = ThreadPoolExecutor(max_workers=32)
        self._history = None
        try:
            from .history import History
            self._history = History()
        except Exception as exc:
            # Monitoring still works; only reports are lost.
            print(f"nettool: history is unavailable: {exc}", file=sys.stderr)
        self._monitor = Monitor(emit, history=self._history)
        # Open management sessions, one per device, each used by one request
        # at a time: an SSH shell cannot interleave two commands.
        self._panels: dict[str, tuple] = {}
        self._panels_lock = threading.Lock()

        broker = handshake.get("broker")
        if broker:
            try:
                self._broker = BrokerClient(
                    broker["address"], broker["family"], broker["token"],
                    on_event=self._on_broker_event,
                )
                self._was_connected = True
            except Exception as exc:
                print(f"nettool: could not reach amvault: {exc}", file=sys.stderr)

    # --- amvault --------------------------------------------------------
    def _on_broker_event(self, message: dict) -> None:
        name = {
            "locked": "vault.locked",
            "unlocked": "vault.unlocked",
            "closed": "vault.disconnected",
            "focus": "window.focus",
        }.get(message.get("event"))
        if name:
            self._emit({"event": name})

    def _ask(self, op: str, **params) -> dict:
        if self._broker is None:
            raise EngineError("Not connected to amvault.")
        try:
            return self._broker.request(op, **params)
        except BrokerError as exc:
            raise EngineError(str(exc)) from exc

    # --- methods --------------------------------------------------------
    def app_status(self, _params: dict) -> dict:
        """Where device data comes from, and whether it is available now."""
        if self._broker is not None and not self._broker.closed.is_set():
            try:
                status = self._broker.request("status", timeout=5)
            except BrokerError:
                status = None
            if status is not None:
                return {"source": "vault", "locked": status["locked"], "vault": status["vault"]}
        if self._demo:
            return {"source": "demo", "locked": False, "vault": None}
        return {
            "source": "disconnected" if self._was_connected else "none",
            "locked": False,
            "vault": None,
        }

    def devices_list(self, _params: dict) -> dict:
        if self._broker is None and self._demo:
            devices = [dict(device) for device in demo.DEVICES]
        else:
            devices = self._ask("list_devices")["devices"]
        for device in devices:
            device["platform"] = platforms.detect(device)
        self._identify(devices)
        for device in devices:
            device["remote"] = remote.available(device)
        # Remembered so a locked vault does not stop monitoring from starting;
        # the list holds no secrets.
        self._devices = devices
        return {"devices": devices}

    def _identify(self, devices: list[dict]) -> None:
        """Work out what the unlabelled devices are from their SSH greeting.

        Only for entries that say nothing about their vendor, and only real
        ones: what the entry says always wins.  Answers are remembered, so a
        device is asked once; one that did not answer is asked again next time.
        """
        if self._broker is None:
            return

        def port_of(device: dict) -> int:
            fields = device.get("fields") or {}
            port = str(fields.get("port") or fields.get("mgmt_port") or "")
            return int(port) if port.isdigit() else 22

        unknown = [d for d in devices if d["platform"] == platforms.UNKNOWN and d.get("address")]
        asking = [d for d in unknown if (d["address"], port_of(d)) not in self._identified]
        banners = self._pings.map(lambda d: net.ssh_banner(d["address"], port_of(d)), asking)
        for device, banner in zip(asking, banners):
            platform = platforms.from_ssh_banner(banner)
            if platform != platforms.UNKNOWN:
                self._identified[(device["address"], port_of(device))] = platform
        for device in unknown:
            found = self._identified.get((device["address"], port_of(device)))
            if found:
                device["platform"] = found
                device["platform_detected"] = True

    def ssh_open(self, params: dict) -> dict:
        """Open a terminal session to a device.

        amvault does the launching, in the user's own terminal, so the key or
        password goes straight from the vault to ssh and never passes through
        this process.
        """
        if self._broker is None and self._demo:
            raise EngineError("These are sample devices; there is nothing to connect to.")
        return self._ask("ssh", device=str(params.get("device") or ""))

    def devices_probe(self, params: dict) -> dict:
        """Ping a list of addresses at once: {address: {"up", "ms"}}."""
        addresses = [str(a) for a in (params.get("addresses") or []) if a][:512]
        results = self._pings.map(net.ping, addresses)
        return {"reach": dict(zip(addresses, results))}

    def devices_add(self, params: dict) -> dict:
        """Save a discovered host to the vault as a new device entry.

        Only its name and what the scan saw are sent; the sign-in details are
        filled in by the person, in amvault.
        """
        if self._broker is None and self._demo:
            raise EngineError("Sample data cannot be changed. Open the tool from amvault to save devices.")
        fields = params.get("fields")
        return self._ask(
            "add_device",
            title=str(params.get("title") or ""),
            fields=fields if isinstance(fields, dict) else {},
        )

    def net_networks(self, _params: dict) -> dict:
        return {"networks": net.local_networks()}

    def scan_start(self, params: dict) -> dict:
        try:
            return self._scanner.start(str(params.get("cidr") or ""))
        except net.NetError as exc:
            raise EngineError(str(exc)) from exc

    def scan_cancel(self, _params: dict) -> dict:
        return self._scanner.cancel()

    def monitor_start(self, params: dict) -> dict:
        """Watch the given devices (and stop watching any others).

        Their credentials are fetched from the vault now, once, so the vault
        can lock afterwards without interrupting the dashboard.
        """
        wanted = [str(i) for i in (params.get("devices") or [])]
        try:
            listed = self.devices_list({})["devices"]
        except EngineError:
            # Locked or disconnected: watch what we already know about, by ping.
            listed = self._devices
        known = {d["id"]: d for d in listed}
        devices = [known[i] for i in wanted if i in known]

        def credentials(device_id: str) -> dict:
            if self._broker is None:
                raise EngineError("sample devices have nothing to sign in to.")
            return self._ask("get_credentials", device=device_id)["fields"]

        return self._monitor.start(devices, credentials)

    def monitor_stop(self, _params: dict) -> dict:
        return self._monitor.stop()

    def monitor_state(self, _params: dict) -> dict:
        return self._monitor.state()

    def _report_hours(self, params: dict) -> float:
        try:
            return min(max(float(params.get("hours") or 24), 0.1), 24 * 31)
        except (TypeError, ValueError):
            return 24.0

    def report_summary(self, params: dict) -> dict:
        if self._history is None:
            raise EngineError("History could not be opened, so there is nothing to report.")
        return {"rows": self._history.summary(self._report_hours(params))}

    def report_export(self, params: dict) -> dict:
        if self._history is None:
            raise EngineError("History could not be opened, so there is nothing to export.")
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        directory = downloads if os.path.isdir(downloads) else os.path.expanduser("~")
        try:
            return {"path": self._history.export_csv(self._report_hours(params), directory)}
        except OSError as exc:
            raise EngineError(f"Could not write the report: {exc}") from exc

    def remote_open(self, params: dict) -> dict:
        """Start a remote-desktop program pointed at a device."""
        device_id, kind = str(params.get("device") or ""), str(params.get("kind") or "")
        if self._broker is None and self._demo:
            raise EngineError("These are sample devices; there is nothing to connect to.")
        try:
            listed = self.devices_list({})["devices"]
        except EngineError:
            listed = self._devices       # RustDesk needs nothing from a locked vault
        device = next((d for d in listed if d["id"] == device_id), None)
        if device is None:
            raise EngineError("That device is no longer in the vault.")
        try:
            if kind == "rustdesk":
                return remote.open_rustdesk(device)
            if kind == "rdp":
                credentials = self._ask("get_credentials", device=device_id)["fields"]
                return remote.open_rdp(device, credentials)
        except remote.RemoteError as exc:
            raise EngineError(str(exc)) from exc
        except OSError as exc:
            raise EngineError(f"Could not start the remote desktop program: {exc}") from exc
        raise EngineError(f"Unknown kind of remote desktop: {kind}")

    # --- management panels ----------------------------------------------
    def _panel(self, params: dict):
        entry = self._panels.get(str(params.get("device") or ""))
        if entry is None:
            raise EngineError("The session to this device is closed. Open the panel again.")
        return entry

    def _with_panel(self, params: dict, work):
        panel, lock = self._panel(params)
        try:
            with lock:
                return work(panel)
        except PanelError as exc:
            raise EngineError(str(exc)) from exc
        except SshError as exc:
            # The connection is gone; drop it so the next open starts clean.
            self.panel_close(params)
            raise EngineError(str(exc)) from exc

    def panel_open(self, params: dict) -> dict:
        """Sign in to a device for managing it. Needs the vault unlocked."""
        device_id = str(params.get("device") or "")
        existing = self._panels.get(device_id)
        if existing is not None:
            return existing[0].describe()
        if self._broker is None and self._demo:
            raise EngineError("These are sample devices; there is nothing to sign in to.")
        device = next((d for d in self.devices_list({})["devices"] if d["id"] == device_id), None)
        if device is None:
            raise EngineError("That device is no longer in the vault.")
        credentials = self._ask("get_credentials", device=device_id)["fields"]
        try:
            panel = open_panel(device, credentials)
        except (PanelError, SshError) as exc:
            raise EngineError(str(exc)) from exc
        with self._panels_lock:
            self._panels[device_id] = (panel, threading.Lock())
        return panel.describe()

    def panel_read(self, params: dict) -> dict:
        return self._with_panel(params, lambda p: p.read(str(params.get("section") or "")))

    def _change(self, params: dict):
        return (str(params.get("section") or ""), str(params.get("action") or ""),
                params.get("row"), params.get("values") or {})

    def panel_plan(self, params: dict) -> dict:
        return self._with_panel(params, lambda p: p.plan(*self._change(params)))

    def panel_apply(self, params: dict) -> dict:
        return self._with_panel(params, lambda p: p.apply(*self._change(params)))

    def panel_close(self, params: dict) -> dict:
        with self._panels_lock:
            entry = self._panels.pop(str(params.get("device") or ""), None)
        if entry is not None:
            entry[0].close()
        return {}

    METHODS = {
        "app.status": app_status,
        "remote.open": remote_open,
        "panel.open": panel_open,
        "panel.read": panel_read,
        "panel.plan": panel_plan,
        "panel.apply": panel_apply,
        "panel.close": panel_close,
        "monitor.start": monitor_start,
        "monitor.stop": monitor_stop,
        "monitor.state": monitor_state,
        "report.summary": report_summary,
        "report.export": report_export,
        "devices.list": devices_list,
        "devices.probe": devices_probe,
        "devices.add": devices_add,
        "ssh.open": ssh_open,
        "net.networks": net_networks,
        "scan.start": scan_start,
        "scan.cancel": scan_cancel,
    }

    def dispatch(self, method: str, params: dict) -> dict:
        handler = self.METHODS.get(method)
        if handler is None:
            raise EngineError(f"Unknown method: {method}")
        return handler(self, params)


def main() -> int:
    # Keep our own handle on the protocol stream and point `print` at stderr,
    # so a stray print in a library cannot corrupt a response.
    out = sys.stdout
    sys.stdout = sys.stderr
    write_lock = threading.Lock()

    def emit(message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        with write_lock:
            try:
                out.write(line + "\n")
                out.flush()
            except (OSError, ValueError):
                pass  # the shell has gone; stdin closing will end us shortly

    try:
        handshake = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        handshake = {}
    engine = Engine(handshake if isinstance(handshake, dict) else {}, emit)

    def serve(request: dict) -> None:
        request_id = request.get("id")
        try:
            params = request.get("params")
            result = engine.dispatch(
                str(request.get("method")), params if isinstance(params, dict) else {}
            )
            emit({"id": request_id, "result": result})
        except EngineError as exc:
            emit({"id": request_id, "error": str(exc)})
        except Exception as exc:
            emit({"id": request_id, "error": f"{type(exc).__name__}: {exc}"})

    # A pool, so one slow device does not hold up the rest of the window.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if isinstance(request, dict):
                pool.submit(serve, request)
    return 0


if __name__ == "__main__":
    sys.exit(main())
