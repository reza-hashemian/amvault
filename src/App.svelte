<script lang="ts">
  import { onMount } from "svelte";
  import { appWindow, call, onEngineEvent } from "./lib/api";
  import DeviceIcon from "./lib/DeviceIcon.svelte";
  import GuideList from "./lib/GuideList.svelte";
  import { guideFor } from "./lib/guides";
  import Inspector from "./lib/Inspector.svelte";
  import { monitor } from "./lib/monitor.svelte";
  import MonitorView from "./lib/MonitorView.svelte";
  import PanelView from "./lib/PanelView.svelte";
  import { scan } from "./lib/scan.svelte";
  import ScanView from "./lib/ScanView.svelte";
  import Sidebar from "./lib/Sidebar.svelte";
  import {
    PLATFORM_LABELS, type Device, type Reach, type ScanHost, type Status, type View,
  } from "./lib/types";

  // How often the device list re-checks who is answering.
  const PROBE_EVERY_MS = 20_000;

  let status = $state<Status | null>(null);
  let devices = $state<Device[]>([]);
  let loading = $state(true);
  let problem = $state("");
  let view = $state<View>({ kind: "devices", platform: null });
  let query = $state("");
  let selectedId = $state<string | null>(null);
  let connecting = $state<string | null>(null);
  let reach = $state<Record<string, Reach>>({});
  let adding = $state<string | null>(null);
  let notice = $state<{ text: string; bad: boolean } | null>(null);
  let maximized = $state(false);
  let noticeTimer: ReturnType<typeof setTimeout> | undefined;

  const selected = $derived(devices.find((device) => device.id === selectedId) ?? null);

  const visible = $derived.by(() => {
    if (view.kind !== "devices") return [];
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const platform = view.platform;
    return devices.filter((device) => {
      if (platform && device.platform !== platform) return false;
      const text = [device.title, device.address, ...device.tags, ...Object.values(device.fields)]
        .join(" ")
        .toLowerCase();
      return words.every((word) => text.includes(word));
    });
  });

  const managed = $derived(
    view.kind === "panel" ? devices.find((device) => device.id === (view as { device: string }).device) ?? null : null,
  );

  const heading = $derived(
    view.kind === "panel"
      ? managed?.title ?? "Device"
      : view.kind === "guide"
      ? `${guideFor(view.platform)?.label ?? ""} commands`
      : view.kind === "scan"
        ? "Scan network"
        : view.kind === "monitor"
          ? "Dashboard"
        : view.platform
          ? PLATFORM_LABELS[view.platform]
          : "All devices",
  );

  function say(text: string, bad = false) {
    notice = { text, bad };
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => (notice = null), bad ? 7000 : 3500);
  }

  async function refresh() {
    loading = true;
    try {
      status = await call<Status>("app.status");
      if (status.source === "none" || status.source === "disconnected" || status.locked) {
        // Keep what is on screen: the list holds no secrets, and blanking it
        // every time the vault auto-locks would be needlessly disruptive.
        problem = "";
        return;
      }
      devices = (await call<{ devices: Device[] }>("devices.list")).devices;
      problem = "";
      void probe();
    } catch (error) {
      problem = (error as Error).message;
    } finally {
      loading = false;
    }
  }

  /** Ping every device that has an address. Failing quietly is fine: the
      dots simply stay as they were until the next round. */
  async function probe() {
    const addresses = [...new Set(devices.map((device) => device.address).filter(Boolean))];
    if (!addresses.length || document.hidden) return;
    try {
      reach = (await call<{ reach: Record<string, Reach> }>("devices.probe", { addresses })).reach;
    } catch {
      /* keep the last known state */
    }
  }

  async function addHost(host: ScanHost) {
    adding = host.address;
    const vendor = { mikrotik: "MikroTik", windows: "Microsoft" }[host.platform as string];
    try {
      const added = await call<{ device: Device }>("devices.add", {
        // "sw-floor1.office.lan" reads better in a list as "sw-floor1".
        title: host.hostname.split(".")[0] || host.address,
        fields: { mgmt_ip: host.address, ...(vendor ? { vendor } : {}) },
      });
      await refresh();
      say(`Added ${added.device.title}. Set its user and password in amvault.`);
    } catch (error) {
      say((error as Error).message, true);
    } finally {
      adding = null;
    }
  }

  async function openSsh(device: Device) {
    connecting = device.id;
    try {
      const result = await call<{ display: string }>("ssh.open", { device: device.id });
      say(`Opened ${result.display} in your terminal`);
    } catch (error) {
      say((error as Error).message, true);
    } finally {
      connecting = null;
    }
  }

  async function openRemote(device: Device, kind: "rdp" | "rustdesk") {
    try {
      const result = await call<{ display: string }>("remote.open", { device: device.id, kind });
      say(`Opening ${result.display}`);
    } catch (error) {
      say((error as Error).message, true);
    }
  }

  function choose(next: View) {
    view = next;
    query = "";
    if (next.kind !== "devices") selectedId = null;
  }

  onMount(() => {
    void refresh();
    void monitor.sync();
    appWindow.watchMaximized((value) => (maximized = value));
    onEngineEvent((message) => {
      if (message.event.startsWith("scan.")) scan.handle(message);
      else if (message.event.startsWith("monitor.")) monitor.handle(message);
      else if (message.event === "window.focus") void appWindow.focus();
      else void refresh();
    });
    const timer = setInterval(probe, PROBE_EVERY_MS);
    return () => clearInterval(timer);
  });
</script>

<svelte:window
  onkeydown={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "f") {
      event.preventDefault();
      document.querySelector<HTMLInputElement>(".toolbar input")?.focus();
    } else if ((event.ctrlKey || event.metaKey) && event.key === "r") {
      event.preventDefault();
      void refresh();
    } else if (event.key === "Escape") {
      selectedId = null;
    }
  }}
/>

<div class="window" class:maximized>
  <Sidebar {devices} {status} {view} watching={monitor.watching.length} onselect={choose} />

  <main>
    <div class="toolbar" data-tauri-drag-region>
      <div class="titles" data-tauri-drag-region>
        <h1 data-tauri-drag-region>{heading}</h1>
        {#if view.kind === "devices" && devices.length}
          <span data-tauri-drag-region>{visible.length} of {devices.length}</span>
        {/if}
      </div>
      {#if view.kind === "devices"}
        <button class="tool" aria-label="Reload devices" title="Reload  (Ctrl+R)"
                disabled={loading} onclick={refresh}>
          <svg viewBox="0 0 16 16" class:spinning={loading}>
            <path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.75M13.2 2.6v2.9h-2.9" />
          </svg>
        </button>
      {/if}
      <label class="search" class:hidden={view.kind === "monitor"}>
        <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.2" /><path d="m10.2 10.2 3.2 3.2" /></svg>
        <input type="search" bind:value={query}
               placeholder={view.kind === "guide" ? "Search commands" : view.kind === "scan" ? "Search results" : view.kind === "panel" ? "Search this table" : "Search devices"} />
      </label>
    </div>

    {#if view.kind === "panel"}
      {#if managed}
        {#key managed.id}
          <PanelView device={managed} {query} onnotice={say}
                     onback={() => choose({ kind: "devices", platform: null })} />
        {/key}
      {:else}
        <div class="blank"><h2>That device is no longer in the vault</h2></div>
      {/if}
    {:else if view.kind === "monitor"}
      <MonitorView {devices} {status} onnotice={say} />
    {:else if view.kind === "scan"}
      <ScanView {devices} {query} {adding} onadd={addHost} />
    {:else if view.kind === "guide"}
      <div class="page">
        <div class="reading">
          <GuideList platform={view.platform} {query} />
        </div>
      </div>
    {:else if problem}
      <div class="blank">
        <h2>Could not load devices</h2>
        <p class="selectable">{problem}</p>
        <button class="button" onclick={refresh}>Try again</button>
      </div>
    {:else if status?.source === "none"}
      <div class="blank">
        <h2>Open this from amvault</h2>
        <p>Devices and their sign-in details stay in your vault. In amvault, choose Network tool and this window fills in without asking for your password again.</p>
        <p>The command guides in the sidebar work without a vault.</p>
      </div>
    {:else if devices.length === 0 && status?.locked}
      <div class="blank">
        <h2>The vault is locked</h2>
        <p>Unlock amvault and your devices appear here.</p>
      </div>
    {:else if devices.length === 0 && status?.source === "disconnected"}
      <div class="blank">
        <h2>amvault was closed</h2>
        <p>Start amvault and choose Network tool to reconnect.</p>
      </div>
    {:else if devices.length === 0 && !loading}
      <div class="blank">
        <h2>No devices in the vault yet</h2>
        <p>In amvault, add an entry in the Network Device category and give it an mgmt_ip. SSH key entries with a host show up here too.</p>
      </div>
    {:else}
      <div class="table" class:compact={selected !== null} role="grid" aria-label="Devices">
        <div class="head" role="row">
          <span role="columnheader">Name</span>
          <span role="columnheader">Address</span>
          <span role="columnheader">Platform</span>
          <span role="columnheader">Model</span>
          <span role="columnheader">Location</span>
          <span role="columnheader">Ping</span>
        </div>
        <div class="rows">
          {#each visible as device (device.id)}
            {@const state = reach[device.address]}
            <button class="line" role="row" class:selected={device.id === selectedId}
                    onclick={() => (selectedId = device.id)}
                    ondblclick={() => openSsh(device)}>
              <span class="name" role="gridcell">
                <i class:up={state?.up} class:down={state && !state.up}
                   title={state ? (state.up ? "Answering ping" : "Not answering ping") : "Not checked yet"}></i>
                <DeviceIcon {device} /><span>{device.title}</span>
              </span>
              <span class="mono" role="gridcell">{device.address || "—"}</span>
              <span role="gridcell">{device.platform === "unknown" ? device.fields.vendor ?? "—" : PLATFORM_LABELS[device.platform]}</span>
              <span role="gridcell">{device.fields.model ?? "—"}</span>
              <span role="gridcell">{device.fields.location ?? "—"}</span>
              <span class="ping" role="gridcell">{state ? (state.up ? `${state.ms} ms` : "No reply") : "—"}</span>
            </button>
          {:else}
            <p class="none">No device matches “{query}”.</p>
          {/each}
        </div>
      </div>
    {/if}

    {#if notice}
      <div class="notice" class:bad={notice.bad} role="status">{notice.text}</div>
    {/if}
  </main>

  {#if selected && view.kind === "devices"}
    <Inspector device={selected} {status} reach={reach[selected.address]} busy={connecting === selected.id}
               onssh={openSsh} onremote={openRemote} onmanage={(device) => choose({ kind: "panel", device: device.id })}
               onclose={() => (selectedId = null)} />
  {/if}
</div>

<style>
  .window {
    height: 100%;
    display: flex;
    overflow: hidden;
    background: var(--window);
    border-radius: var(--radius-window);
    /* The light inner edge every macOS window has. */
    box-shadow: inset 0 0 0 1px var(--edge);
  }

  .window.maximized {
    border-radius: 0;
    box-shadow: none;
  }

  main {
    position: relative;
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .toolbar {
    height: 52px;
    flex: none;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 16px 0 20px;
    border-bottom: 1px solid var(--hairline);
  }

  .titles {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }

  h1 {
    margin: 0;
    font-size: 15px;
    font-weight: 650;
    letter-spacing: -0.01em;
    white-space: nowrap;
  }

  .titles span {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .tool {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    display: grid;
    place-items: center;
  }

  .tool:hover:not(:disabled) {
    background: var(--hover);
  }

  .tool svg,
  .search svg {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: var(--text-2);
    stroke-width: 1.3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .spinning {
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .search {
    width: 210px;
    height: 28px;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 8px;
    border-radius: 7px;
    background: var(--field);
  }

  .search.hidden {
    visibility: hidden;
  }

  .search:focus-within {
    outline: 3px solid color-mix(in srgb, var(--accent) 55%, transparent);
  }

  .search input {
    flex: 1;
    min-width: 0;
    border: 0;
    background: none;
    user-select: text;
  }

  .search input::-webkit-search-cancel-button {
    -webkit-appearance: none;
  }

  /* --- device table --- */
  .table {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .head,
  .line {
    display: grid;
    grid-template-columns: minmax(160px, 2.2fr) minmax(104px, 1.2fr) minmax(74px, 1fr) minmax(80px, 1.5fr) minmax(70px, 1.3fr) 62px;
    align-items: center;
    gap: 14px;
    padding: 0 12px;
  }

  /* With the details pane open the model and location are shown there, so
     the list gives their room to the name. */
  .compact .head,
  .compact .line {
    grid-template-columns: minmax(160px, 2.2fr) minmax(104px, 1.2fr) minmax(74px, 1fr) 62px;
  }

  .compact .head > :nth-child(4),
  .compact .head > :nth-child(5),
  .compact .line > :nth-child(4),
  .compact .line > :nth-child(5) {
    display: none;
  }

  .head {
    height: 28px;
    flex: none;
    margin: 0 8px;
    font-size: 11px;
    font-weight: 500;
    color: var(--text-2);
    border-bottom: 1px solid var(--hairline);
  }

  .rows {
    flex: 1;
    overflow-y: auto;
    padding: 4px 8px 12px;
  }

  .line {
    width: 100%;
    height: 34px;
    border-radius: 6px;
    text-align: left;
  }

  .line > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-2);
  }

  .line:nth-child(even) {
    background: var(--stripe);
  }

  .line .name {
    display: flex;
    align-items: center;
    gap: 9px;
    color: var(--text);
    font-weight: 500;
  }

  .name span {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Whether the device answered the last ping. Hollow until first checked. */
  .name i {
    width: 8px;
    height: 8px;
    flex: none;
    border-radius: 50%;
    box-shadow: inset 0 0 0 1.5px var(--text-3);
  }

  .name i.up {
    background: var(--green);
    box-shadow: none;
  }

  .name i.down {
    background: var(--red);
    box-shadow: none;
  }

  .line.selected .name i {
    box-shadow: 0 0 0 1.5px var(--on-accent);
  }

  .ping {
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  .head span:last-child {
    text-align: right;
  }

  .name :global(svg) {
    flex: none;
    color: var(--text-2);
  }

  .line.selected {
    background: var(--accent);
  }

  .line.selected > span,
  .line.selected .name :global(svg) {
    color: var(--on-accent);
  }

  .none {
    margin: 40px 0;
    text-align: center;
    color: var(--text-2);
  }

  /* --- guide --- */
  .page {
    flex: 1;
    overflow-y: auto;
    padding: 26px 32px 48px;
  }

  .reading {
    max-width: 760px;
    margin: 0 auto;
  }

  /* --- empty and blocked states --- */
  .blank {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 0 40px 60px;
    text-align: center;
  }

  .blank h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 650;
  }

  .blank p {
    margin: 0;
    max-width: 46ch;
    line-height: 1.55;
    color: var(--text-2);
  }

  .blank .button {
    margin-top: 10px;
  }

  .notice {
    position: absolute;
    left: 50%;
    bottom: 22px;
    transform: translateX(-50%);
    max-width: min(560px, calc(100% - 48px));
    padding: 9px 16px;
    border-radius: 10px;
    background: var(--raised);
    box-shadow: 0 0 0 0.5px var(--edge), 0 8px 28px rgba(0, 0, 0, 0.35);
    line-height: 1.45;
    animation: rise 0.18s ease-out;
  }

  .notice.bad {
    box-shadow: 0 0 0 1px var(--red), 0 8px 28px rgba(0, 0, 0, 0.35);
  }

  @keyframes rise {
    from { opacity: 0; transform: translate(-50%, 6px); }
  }
</style>
