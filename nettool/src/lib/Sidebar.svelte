<script lang="ts">
  import { appWindow } from "./api";
  import { guides } from "./guides";
  import { PLATFORM_LABELS, type Device, type Platform, type Status, type View } from "./types";

  let {
    devices,
    status,
    view,
    watching = 0,
    onselect,
  }: {
    devices: Device[];
    status: Status | null;
    view: View;
    /** How many devices are being monitored right now. */
    watching?: number;
    onselect: (view: View) => void;
  } = $props();

  // Only the platforms that actually occur, in a stable order.
  const platforms = $derived(
    (Object.keys(PLATFORM_LABELS) as Platform[])
      .map((platform) => ({
        platform,
        count: devices.filter((device) => device.platform === platform).length,
      }))
      .filter((row) => row.count > 0),
  );

  const connection = $derived.by(() => {
    if (!status) return { tone: "idle", title: "Starting…", detail: "" };
    if (status.source === "vault") {
      return status.locked
        ? { tone: "warn", title: "Vault locked", detail: "Unlock amvault to connect" }
        : { tone: "ok", title: status.vault ?? "amvault", detail: "Connected to amvault" };
    }
    if (status.source === "demo") return { tone: "idle", title: "Sample data", detail: "Not from a vault" };
    if (status.source === "disconnected") {
      return { tone: "bad", title: "amvault closed", detail: "Reopen from amvault" };
    }
    return { tone: "idle", title: "No vault", detail: "Open from amvault" };
  });
</script>

<aside class="sidebar">
  <div class="lights" data-tauri-drag-region>
    <button class="light close" aria-label="Close" onclick={appWindow.close}>
      <svg viewBox="0 0 12 12"><path d="M3.6 3.6l4.8 4.8M8.4 3.6l-4.8 4.8" /></svg>
    </button>
    <button class="light minimize" aria-label="Minimize" onclick={appWindow.minimize}>
      <svg viewBox="0 0 12 12"><path d="M2.8 6h6.4" /></svg>
    </button>
    <button class="light zoom" aria-label="Zoom" onclick={appWindow.toggleMaximize}>
      <svg viewBox="0 0 12 12"><path d="M6 3v6M3 6h6" /></svg>
    </button>
  </div>

  <nav>
    <h2>Devices</h2>
    <button
      class="row"
      class:selected={(view.kind === "devices" && view.platform === null) || view.kind === "panel"}
      onclick={() => onselect({ kind: "devices", platform: null })}
    >
      <span class="label">All devices</span>
      <span class="count">{devices.length}</span>
    </button>
    {#each platforms as row (row.platform)}
      <button
        class="row"
        class:selected={view.kind === "devices" && view.platform === row.platform}
        onclick={() => onselect({ kind: "devices", platform: row.platform })}
      >
        <span class="label">{PLATFORM_LABELS[row.platform]}</span>
        <span class="count">{row.count}</span>
      </button>
    {/each}

    <h2>Monitor</h2>
    <button class="row" class:selected={view.kind === "monitor"} onclick={() => onselect({ kind: "monitor" })}>
      <span class="label">Dashboard</span>
      {#if watching}<span class="count live">{watching}</span>{/if}
    </button>

    <h2>Discover</h2>
    <button class="row" class:selected={view.kind === "scan"} onclick={() => onselect({ kind: "scan" })}>
      <span class="label">Scan network</span>
    </button>

    <h2>Command guide</h2>
    {#each guides as guide (guide.platform)}
      <button
        class="row"
        class:selected={view.kind === "guide" && view.platform === guide.platform}
        onclick={() => onselect({ kind: "guide", platform: guide.platform })}
      >
        <span class="label">{guide.label}</span>
      </button>
    {/each}
  </nav>

  <div class="connection">
    <span class="dot {connection.tone}"></span>
    <div>
      <div class="title">{connection.title}</div>
      {#if connection.detail}<div class="detail">{connection.detail}</div>{/if}
    </div>
  </div>
</aside>

<style>
  .sidebar {
    width: 216px;
    flex: none;
    display: flex;
    flex-direction: column;
    background: var(--sidebar);
    border-right: 1px solid var(--hairline);
  }

  .lights {
    height: 52px;
    flex: none;
    display: flex;
    align-items: center;
    gap: 8px;
    padding-left: 19px;
  }

  .light {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    box-shadow: inset 0 0 0 0.5px rgba(0, 0, 0, 0.18);
  }

  .light.close { background: #ff5f57; }
  .light.minimize { background: #febc2e; }
  .light.zoom { background: #28c840; }

  .light svg {
    width: 12px;
    height: 12px;
    stroke: rgba(0, 0, 0, 0.55);
    stroke-width: 1.3;
    stroke-linecap: round;
    opacity: 0;
  }

  /* As on a Mac: the glyphs appear for all three when the pointer is near. */
  .lights:hover .light svg,
  .light:focus-visible svg {
    opacity: 1;
  }

  nav {
    flex: 1;
    overflow-y: auto;
    padding: 0 10px 10px;
  }

  h2 {
    margin: 14px 0 4px;
    padding: 0 8px;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-3);
  }

  nav h2:first-child {
    margin-top: 2px;
  }

  .row {
    width: 100%;
    height: 28px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 8px;
    border-radius: 6px;
    text-align: left;
  }

  .row:hover {
    background: var(--hover);
  }

  .row.selected {
    background: var(--field);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
  }

  .label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .count {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .count.live {
    color: var(--green);
  }

  .connection {
    flex: none;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 18px 14px;
    border-top: 1px solid var(--hairline);
  }

  .dot {
    width: 8px;
    height: 8px;
    flex: none;
    border-radius: 50%;
    background: var(--text-3);
  }

  .dot.ok { background: var(--green); }
  .dot.warn { background: var(--orange); }
  .dot.bad { background: var(--red); }

  .title {
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 150px;
  }

  .detail {
    font-size: 11px;
    color: var(--text-2);
  }
</style>
