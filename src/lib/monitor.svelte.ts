// What is being monitored and what has been measured, kept outside any
// component so the charts are still there after visiting another view.
import { call } from "./api";
import type { Device, EngineEvent, Iface, Sample } from "./types";

const PICKED_KEY = "nettool.monitor.picked";
// An hour at one reading every five seconds.
const KEEP = 720;

interface State {
  tick: number;
  watching: { device: Device; samples: Sample[] }[];
}

class MonitorStore {
  watching = $state<Device[]>([]);
  series = $state<Record<string, Sample[]>>({});
  /** The newest interface list per device; only the latest is ever shown. */
  ifaces = $state<Record<string, Iface[]>>({});
  /** Device ids ticked in the picker. Remembered between runs; ids are not secret. */
  picked = $state<string[]>(readPicked());
  busy = $state(false);
  problem = $state("");

  private adopt(state: State) {
    this.watching = state.watching.map((entry) => entry.device);
    this.series = Object.fromEntries(state.watching.map((entry) => [entry.device.id, entry.samples]));
    const kept: Record<string, Iface[]> = {};
    for (const device of this.watching) if (this.ifaces[device.id]) kept[device.id] = this.ifaces[device.id];
    this.ifaces = kept;
  }

  /** Pick up monitoring that is already running, e.g. after a reload. */
  async sync() {
    try {
      this.adopt(await call<State>("monitor.state"));
    } catch {
      /* the dashboard just starts empty */
    }
  }

  async start() {
    this.busy = true;
    this.problem = "";
    try {
      this.adopt(await call<State>("monitor.start", { devices: this.picked }));
      try {
        localStorage.setItem(PICKED_KEY, JSON.stringify(this.picked));
      } catch {
        /* remembering the choice is a convenience only */
      }
    } catch (error) {
      this.problem = (error as Error).message;
    } finally {
      this.busy = false;
    }
  }

  async stop() {
    try {
      this.adopt(await call<State>("monitor.stop"));
    } catch (error) {
      this.problem = (error as Error).message;
    }
  }

  handle(message: EngineEvent) {
    if (message.event !== "monitor.sample") return;
    const id = message.device as string;
    const sample = message.sample as Sample;
    const series = this.series[id];
    if (!series) return; // a last reading from a device we just stopped watching
    if (sample.ifaces) this.ifaces[id] = sample.ifaces;
    series.push({ ...sample, ifaces: null });
    if (series.length > KEEP) series.splice(0, series.length - KEEP);
  }
}

function readPicked(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(PICKED_KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

export const monitor = new MonitorStore();
