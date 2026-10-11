export type Platform = "cisco_ios" | "mikrotik" | "linux" | "windows" | "unknown";

export interface Device {
  id: string;
  title: string;
  category: string;
  tags: string[];
  address: string;
  platform: Platform;
  /** True when the platform was read from the device, not from its entry. */
  platform_detected?: boolean;
  has_password: boolean;
  has_key: boolean;
  /** Kinds of remote desktop this entry supports. */
  remote?: ("rdp" | "rustdesk")[];
  fields: Record<string, string>;
}

/** Where device data comes from right now. */
export interface Status {
  source: "vault" | "demo" | "disconnected" | "none";
  locked: boolean;
  vault: string | null;
}

export interface GuideItem {
  id: string;
  title: string;
  summary: string;
  /** The mode the command is typed in, as the device shows it. */
  prompt?: string;
  /** The general form, with <placeholders>. */
  syntax: string[];
  params?: { name: string; desc: string }[];
  /** A complete sequence that can be pasted as is. */
  example: string[];
  note?: string;
}

export interface Guide {
  platform: Platform;
  label: string;
  groups: { title: string; items: GuideItem[] }[];
}

export const PLATFORM_LABELS: Record<Platform, string> = {
  cisco_ios: "Cisco IOS",
  mikrotik: "MikroTik",
  linux: "Linux",
  windows: "Windows",
  unknown: "Other",
};

/** What the main area is showing, chosen in the sidebar. */
export type View =
  | { kind: "devices"; platform: Platform | null }
  | { kind: "scan" }
  | { kind: "monitor" }
  | { kind: "panel"; device: string }
  | { kind: "guide"; platform: Platform };

/** The result of one ping. */
export interface Reach {
  up: boolean;
  ms: number | null;
}

/** A network this computer is attached to. */
export interface Network {
  cidr: string;
  /** The part of it one scan covers: all of it, or the /24 we sit in. */
  scan: string;
  address: string;
  interface: string;
  physical: boolean;
}

/** A host a scan found. */
export interface ScanHost {
  address: string;
  /** null when it ignored ping and was seen only in the neighbour table. */
  ms: number | null;
  mac: string;
  hostname: string;
  ports: number[];
  platform: Platform | "";
}

/** One interface of a monitored device, at the latest reading. */
export interface Iface {
  name: string;
  up: boolean;
  main: boolean;
  rx_bps: number | null;
  tx_bps: number | null;
}

/** One reading of a monitored device. Metrics are null on ticks they were not read. */
export interface Sample {
  t: number;
  up: boolean;
  ms: number | null;
  cpu: number | null;
  mem_used: number | null;
  mem_total: number | null;
  uptime: number | null;
  rx_bps: number | null;
  tx_bps: number | null;
  ifaces: Iface[] | null;
  /** Why there are no metrics, when there are none. */
  note: string;
}

export interface ReportRow {
  device: string;
  title: string;
  address: string;
  samples: number;
  availability: number;
  ms_avg: number | null;
  ms_max: number | null;
  cpu_avg: number | null;
  cpu_max: number | null;
  mem_avg: number | null;
  rx_avg: number | null;
  tx_avg: number | null;
}

export interface EngineEvent {
  event: string;
  [key: string]: unknown;
}
