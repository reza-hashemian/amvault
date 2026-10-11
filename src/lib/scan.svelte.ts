// The network scan's state, kept outside any component so results survive
// switching to another view and back.
import { call } from "./api";
import type { EngineEvent, Network, ScanHost } from "./types";

class Scan {
  networks = $state<Network[]>([]);
  range = $state("");
  hosts = $state<Record<string, ScanHost>>({});
  running = $state(false);
  done = $state(0);
  total = $state(0);
  /** The range the results on screen belong to. */
  scanned = $state("");
  problem = $state("");

  /** Hosts in address order, so .9 comes before .10. */
  get sorted(): ScanHost[] {
    const key = (address: string) =>
      address.split(".").reduce((sum, part) => sum * 256 + Number(part), 0);
    return Object.values(this.hosts).sort((a, b) => key(a.address) - key(b.address));
  }

  async loadNetworks() {
    if (this.networks.length) return;
    try {
      this.networks = (await call<{ networks: Network[] }>("net.networks")).networks;
      if (!this.range && this.networks.length) this.range = this.networks[0].scan;
    } catch (error) {
      this.problem = (error as Error).message;
    }
  }

  async start() {
    this.problem = "";
    try {
      const started = await call<{ cidr: string; total: number }>("scan.start", { cidr: this.range });
      this.hosts = {};
      this.scanned = started.cidr;
      this.total = started.total;
      this.done = 0;
      this.running = true;
    } catch (error) {
      this.problem = (error as Error).message;
    }
  }

  stop() {
    void call("scan.cancel");
  }

  handle(message: EngineEvent) {
    if (message.event === "scan.host") {
      const host = message.host as ScanHost;
      this.hosts[host.address] = host;
    } else if (message.event === "scan.progress") {
      this.done = message.done as number;
      this.total = message.total as number;
    } else if (message.event === "scan.done") {
      this.running = false;
    }
  }
}

export const scan = new Scan();
