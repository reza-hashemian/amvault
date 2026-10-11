// The window's only line to the outside: every request goes to the Python
// engine through the shell's single `engine_call` command.
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { EngineEvent } from "./types";

export async function call<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  if (!isTauri()) return preview<T>(method, params);
  try {
    return await invoke<T>("engine_call", { method, params });
  } catch (reason) {
    throw new Error(typeof reason === "string" ? reason : String(reason));
  }
}

/**
 * Stand-in answers for `npm run dev` in a plain browser, where there is no
 * shell and no engine. Lets the interface be worked on with hot reload.
 */
async function preview<T>(method: string, params: Record<string, unknown>): Promise<T> {
  if (method === "app.status") return { source: "demo", locked: false, vault: null } as T;
  if (method === "devices.list") {
    const sample = await import("../../nettool_engine/demo_devices.json");
    return { devices: sample.default } as T;
  }
  if (method === "devices.probe") {
    const reach = Object.fromEntries(
      (params.addresses as string[]).map((address, index) => [
        address,
        index % 4 === 3 ? { up: false, ms: null } : { up: true, ms: 0.4 + index * 1.7 },
      ]),
    );
    return { reach } as T;
  }
  if (method === "net.networks") {
    return {
      networks: [{ cidr: "192.0.2.0/24", scan: "192.0.2.0/24", address: "192.0.2.77", interface: "eth0", physical: true }],
    } as T;
  }
  if (method === "scan.start") {
    const hosts = [
      { address: "192.0.2.1", ms: 0.6, mac: "4c:5e:0c:12:34:56", hostname: "", ports: [22, 80, 8291], platform: "mikrotik" },
      { address: "192.0.2.11", ms: 1.9, mac: "00:1b:54:aa:bb:cc", hostname: "sw-floor1.office.lan", ports: [22, 23], platform: "" },
      { address: "192.0.2.42", ms: 0.3, mac: "3c:52:82:01:02:03", hostname: "reception-pc.office.lan", ports: [445, 3389], platform: "windows" },
      { address: "192.0.2.60", ms: null, mac: "b8:27:eb:0a:0b:0c", hostname: "", ports: [], platform: "" },
      { address: "192.0.2.77", ms: 0.1, mac: "", hostname: "this-computer", ports: [22], platform: "" },
    ];
    hosts.forEach((host, index) =>
      setTimeout(() => {
        previewListeners.forEach((send) => send({ event: "scan.host", host }));
        previewListeners.forEach((send) =>
          send({ event: "scan.progress", done: (index + 1) * 50, total: 254 }),
        );
        if (index === hosts.length - 1) {
          previewListeners.forEach((send) => send({ event: "scan.done", cancelled: false, found: hosts.length }));
        }
      }, 120 * (index + 1)),
    );
    return { cidr: "192.0.2.0/24", total: 254 } as T;
  }
  if (method === "scan.cancel") return {} as T;
  if (method === "panel.open") {
    return {
      platform: "cisco_ios", can_change: true, note: "",
      sections: ["Overview", "Ports", "VLANs", "IP interfaces", "MAC table"].map((label, i) => ({
        id: ["overview", "ports", "vlans", "ip", "mac"][i], label,
      })),
    } as T;
  }
  if (method === "panel.read") {
    if (params.section !== "ports") {
      return {
        columns: [{ key: "item", label: "Item" }, { key: "value", label: "Value" }],
        rows: [["Name", "SW-FLOOR1"], ["Model", "WS-C2960X-48TS-L"], ["Software", "15.2(7)E3"], ["Uptime", "3 weeks, 2 days, 4 hours"], ["Mode", "Privileged (#)"]]
          .map(([item, value]) => ({ id: item, cells: { item, value } })),
        actions: [{ id: "save", label: "Save configuration", scope: "section", fields: [] }],
      } as T;
    }
    const port = (n: number, name: string, status: string, vlan: string) => ({
      id: `Gi0/${n}`,
      cells: { port: `Gi0/${n}`, name, status, vlan, duplex: status === "connected" ? "a-full" : "auto",
               speed: status === "connected" ? "a-1000" : "auto", type: "10/100/1000BaseTX" },
    });
    return {
      columns: [
        { key: "port", label: "Port", mono: true }, { key: "name", label: "Description" },
        { key: "status", label: "Status" }, { key: "vlan", label: "VLAN" },
        { key: "duplex", label: "Duplex" }, { key: "speed", label: "Speed" }, { key: "type", label: "Type" },
      ],
      rows: [
        port(1, "Uplink to core", "connected", "trunk"), port(2, "Reception PC", "connected", "10"),
        port(3, "Reception printer", "connected", "10"), port(4, "", "notconnect", "10"),
        port(5, "Meeting room AP", "connected", "20"), port(6, "", "disabled", "1"),
        port(7, "Camera lobby", "err-disabled", "30"), port(8, "", "notconnect", "1"),
      ],
      actions: [
        { id: "describe", label: "Edit description", scope: "row", fields: [{ name: "description", label: "Description", type: "text", from: "name" }] },
        { id: "vlan", label: "Set access VLAN", scope: "row", fields: [{ name: "vlan", label: "VLAN number", type: "number", from: "vlan" }] },
        { id: "shutdown", label: "Shut down", scope: "row", fields: [], unless: { status: "disabled" } },
        { id: "enable", label: "Bring up", scope: "row", fields: [], when: { status: "disabled" } },
      ],
    } as T;
  }
  if (method === "panel.plan") {
    const values = params.values as Record<string, string>;
    return {
      title: `Put ${params.row} in VLAN ${values.vlan}`,
      commands: ["configure terminal", `interface ${params.row}`, "switchport mode access", `switchport access vlan ${values.vlan}`, "end"],
      warning: "This changes the running configuration only. Use Save configuration on the Overview tab to keep it after a restart.",
    } as T;
  }
  if (method === "panel.apply") return { ok: true, output: "" } as T;
  if (method === "panel.close") return {} as T;
  if (method === "monitor.state") return { tick: 5, watching: previewWatching } as T;
  if (method === "monitor.stop") {
    previewWatching = [];
    return { tick: 5, watching: [] } as T;
  }
  if (method === "monitor.start") {
    const sample = (await import("../../nettool_engine/demo_devices.json")).default;
    const now = Date.now() / 1000;
    previewWatching = sample
      .filter((device) => (params.devices as string[]).includes(device.id))
      .map((device, d) => {
        const metrics = ["linux", "mikrotik", "cisco_ios"].includes(device.platform);
        const samples = Array.from({ length: 90 }, (_, i) => {
          const wave = Math.sin(i / 7 + d) * 0.5 + 0.5;
          const lost = d === 1 && (i === 40 || i === 41);
          return {
            t: now - (90 - i) * 5, up: !lost, ms: lost ? null : 1 + d * 3 + wave * 2,
            cpu: metrics ? 8 + d * 6 + wave * 30 : null,
            mem_used: metrics ? (0.4 + d * 0.08 + wave * 0.05) * 2 ** 30 : null,
            mem_total: metrics ? 2 ** 30 : null, uptime: metrics ? 800000 + i * 5 : null,
            rx_bps: metrics ? (2 + wave * 40) * 1e6 : null, tx_bps: metrics ? (1 + (1 - wave) * 9) * 1e6 : null,
            ifaces: null,
            note: metrics ? "" : "Ping only: reading CPU and traffic is not available for this kind of device yet.",
          };
        });
        return { device, samples };
      });
    return { tick: 5, watching: previewWatching } as T;
  }
  if (method === "report.summary") {
    return {
      rows: previewWatching.map((w, i) => ({
        device: w.device.id, title: w.device.title, address: w.device.address, samples: 17280,
        availability: i === 1 ? 99.62 : 100, ms_avg: 1.8 + i * 3, ms_max: 14 + i * 9,
        cpu_avg: 21 + i * 6, cpu_max: 64 + i * 9, mem_avg: 44 + i * 8, rx_avg: 22e6, tx_avg: 5.5e6,
      })),
    } as T;
  }
  throw new Error("These are sample devices; nothing here can be connected to or changed.");
}

const previewListeners: ((message: EngineEvent) => void)[] = [];
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let previewWatching: { device: any; samples: any[] }[] = [];

/** Things the engine reports without being asked: the vault locking, a scan finding a host. */
export function onEngineEvent(handler: (message: EngineEvent) => void): void {
  if (!isTauri()) {
    previewListeners.push(handler);
    return;
  }
  void listen<EngineEvent>("engine-event", (message) => {
    if (message.payload.event) handler(message.payload);
  });
}

export const appWindow = {
  close: () => isTauri() && getCurrentWindow().close(),
  minimize: () => isTauri() && getCurrentWindow().minimize(),
  toggleMaximize: () => isTauri() && getCurrentWindow().toggleMaximize(),
  async focus() {
    if (!isTauri()) return;
    const current = getCurrentWindow();
    await current.unminimize();
    await current.setFocus();
  },
  /** Calls back with whether the window currently fills the screen. */
  watchMaximized(handler: (maximized: boolean) => void): void {
    if (!isTauri()) return;
    const current = getCurrentWindow();
    const report = () => void current.isMaximized().then(handler);
    report();
    void current.onResized(report);
  },
};

/** Copy text, falling back for webviews that withhold the clipboard API. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  }
}
