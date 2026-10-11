// How measurements are written for people. Units are spelled the way network
// gear spells them: rates in bits, sizes in binary bytes.

export function rate(bps: number | null | undefined): string {
  if (bps == null) return "—";
  if (bps < 1000) return `${Math.round(bps)} b/s`;
  const units = ["kb/s", "Mb/s", "Gb/s"];
  let value = bps / 1000;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function bytes(count: number | null | undefined): string {
  if (count == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = count;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function percent(value: number | null | undefined, digits = 0): string {
  return value == null ? "—" : `${value.toFixed(digits)}%`;
}

export function millis(value: number | null | undefined): string {
  if (value == null) return "—";
  return value < 0.1 ? "<0.1 ms" : `${value < 10 ? value.toFixed(1) : value.toFixed(0)} ms`;
}

/** 3d 4h, 5h 12m, 9m: the two largest units are enough to read at a glance. */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

export function clock(t: number): string {
  return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
