<script lang="ts">
  import { onMount } from "svelte";
  import { call } from "./api";
  import DeviceIcon from "./DeviceIcon.svelte";
  import { millis, percent, rate } from "./format";
  import { monitor } from "./monitor.svelte";
  import MonitorCard from "./MonitorCard.svelte";
  import { PLATFORM_LABELS, type Device, type ReportRow, type Status } from "./types";

  let {
    devices,
    status,
    onnotice,
  }: {
    devices: Device[];
    status: Status | null;
    onnotice: (text: string, bad?: boolean) => void;
  } = $props();

  // Platforms the engine can read CPU, memory and traffic from.
  const READABLE = ["linux", "mikrotik", "cisco_ios", "windows"];
  const PERIODS = [
    { hours: 1, label: "Last hour" },
    { hours: 24, label: "24 hours" },
    { hours: 168, label: "7 days" },
  ];

  let choosing = $state(false);
  let hours = $state(24);
  let report = $state<ReportRow[]>([]);
  let exporting = $state(false);

  const picking = $derived(choosing || monitor.watching.length === 0);
  const pickedHere = $derived(monitor.picked.filter((id) => devices.some((d) => d.id === id)));
  const vaultLocked = $derived(status?.source === "vault" && status.locked);

  function toggle(id: string) {
    monitor.picked = monitor.picked.includes(id)
      ? monitor.picked.filter((other) => other !== id)
      : [...monitor.picked, id];
  }

  async function begin() {
    monitor.picked = pickedHere;
    await monitor.start();
    if (!monitor.problem) {
      choosing = false;
      void loadReport();
    }
  }

  function change() {
    monitor.picked = monitor.watching.map((device) => device.id);
    choosing = true;
  }

  async function loadReport() {
    try {
      report = (await call<{ rows: ReportRow[] }>("report.summary", { hours })).rows;
    } catch {
      report = [];
    }
  }

  async function exportCsv() {
    exporting = true;
    try {
      const { path } = await call<{ path: string }>("report.export", { hours });
      onnotice(`Report saved to ${path}`);
    } catch (error) {
      onnotice((error as Error).message, true);
    } finally {
      exporting = false;
    }
  }

  onMount(() => {
    void monitor.sync().then(loadReport);
    // The report covers hours; refreshing it once a minute is plenty.
    const timer = setInterval(loadReport, 60_000);
    return () => clearInterval(timer);
  });
</script>

{#if picking}
  <div class="picker">
    <div class="intro">
      <h2>Choose what to watch</h2>
      <p>
        Every chosen device is pinged every 5 seconds. For Linux, MikroTik, Cisco IOS and Windows machines the tool also signs in over SSH and reads CPU, memory and port traffic. Windows needs its OpenSSH server turned on.
      </p>
      <p>
        Sign-in details are read from the vault once, when you start. Monitoring then carries on even if the vault locks, until you stop it or close this window.
      </p>
      {#if vaultLocked}
        <p class="warn">The vault is locked, so devices started now are watched by ping only. Unlock amvault first to read CPU and traffic.</p>
      {/if}
    </div>

    {#if devices.length === 0}
      <p class="none">There are no devices to watch yet.</p>
    {:else}
      <div class="list">
        {#each devices as device (device.id)}
          <label class="pick">
            <input type="checkbox" checked={monitor.picked.includes(device.id)}
                   onchange={() => toggle(device.id)} />
            <DeviceIcon {device} />
            <span class="title">{device.title}</span>
            <span class="mono">{device.address || "no address"}</span>
            <span class="reads">
              {READABLE.includes(device.platform)
                ? `${PLATFORM_LABELS[device.platform]}: ping, CPU, memory, traffic`
                : "Ping only"}
            </span>
          </label>
        {/each}
      </div>
    {/if}

    {#if monitor.problem}<p class="problem selectable">{monitor.problem}</p>{/if}

    <div class="actions">
      <button class="button primary" disabled={monitor.busy || pickedHere.length === 0} onclick={begin}>
        {monitor.busy
          ? "Starting…"
          : pickedHere.length
            ? `Start monitoring ${pickedHere.length} ${pickedHere.length === 1 ? "device" : "devices"}`
            : "Start monitoring"}
      </button>
      {#if choosing}
        <button class="button" onclick={() => (choosing = false)}>Cancel</button>
      {/if}
    </div>
  </div>
{:else}
  <div class="dashboard">
    <div class="bar">
      <span>Watching {monitor.watching.length} {monitor.watching.length === 1 ? "device" : "devices"}, charts show the last hour</span>
      <button class="button" onclick={change}>Change devices</button>
      <button class="button" onclick={() => monitor.stop()}>Stop</button>
    </div>

    <div class="cards">
      {#each monitor.watching as device (device.id)}
        <MonitorCard {device} samples={monitor.series[device.id] ?? []} ifaces={monitor.ifaces[device.id] ?? []} />
      {/each}
    </div>

    <section class="report">
      <div class="report-head">
        <h2>Report</h2>
        <div class="segments" role="group" aria-label="Period">
          {#each PERIODS as period}
            <button class:on={hours === period.hours} aria-pressed={hours === period.hours}
                    onclick={() => { hours = period.hours; void loadReport(); }}>
              {period.label}
            </button>
          {/each}
        </div>
        <button class="button" disabled={exporting || report.length === 0} onclick={exportCsv}>
          {exporting ? "Saving…" : "Export CSV"}
        </button>
      </div>

      {#if report.length === 0}
        <p class="none">Nothing recorded in this period yet.</p>
      {:else}
        <div class="table">
          <div class="tr head">
            <span>Device</span><span>Availability</span><span>Ping avg</span><span>Ping max</span>
            <span>CPU avg</span><span>CPU max</span><span>Memory avg</span><span>In avg</span><span>Out avg</span>
          </div>
          {#each report as row (row.device)}
            <div class="tr">
              <span class="name">{row.title}</span>
              <span class:poor={row.availability < 99}>{percent(row.availability, 2)}</span>
              <span>{millis(row.ms_avg)}</span>
              <span>{millis(row.ms_max)}</span>
              <span>{percent(row.cpu_avg)}</span>
              <span>{percent(row.cpu_max)}</span>
              <span>{percent(row.mem_avg)}</span>
              <span>{rate(row.rx_avg)}</span>
              <span>{rate(row.tx_avg)}</span>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .picker,
  .dashboard {
    flex: 1;
    overflow-y: auto;
    padding: 20px 22px 32px;
  }

  .picker {
    max-width: 780px;
  }

  .intro h2 {
    margin: 0 0 8px;
    font-size: 17px;
    font-weight: 650;
  }

  .intro p {
    margin: 0 0 8px;
    max-width: 68ch;
    line-height: 1.55;
    color: var(--text-2);
  }

  .intro .warn {
    color: var(--text);
    padding: 9px 12px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--orange) 12%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--orange) 30%, transparent);
  }

  .list {
    margin: 16px 0;
    border-radius: 10px;
    background: var(--raised);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
  }

  .pick {
    display: grid;
    grid-template-columns: 16px 16px minmax(120px, 1.4fr) minmax(110px, 1fr) minmax(150px, 1.6fr);
    align-items: center;
    gap: 10px;
    min-height: 38px;
    padding: 0 14px;
    color: var(--text-2);
  }

  .pick + .pick {
    border-top: 1px solid var(--hairline);
  }

  .pick span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pick .title {
    color: var(--text);
    font-weight: 500;
  }

  input[type="checkbox"] {
    width: 14px;
    height: 14px;
    margin: 0;
    accent-color: var(--accent);
  }

  .actions {
    display: flex;
    gap: 8px;
  }

  .problem {
    margin: 0 0 12px;
    padding: 9px 12px;
    border-radius: 8px;
    box-shadow: inset 0 0 0 1px var(--red);
    line-height: 1.5;
  }

  .none {
    margin: 22px 0;
    color: var(--text-2);
  }

  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
    color: var(--text-2);
  }

  .bar span {
    flex: 1;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(430px, 1fr));
    gap: 14px;
    align-items: start;
  }

  .report {
    margin-top: 30px;
  }

  .report-head {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
  }

  .report-head h2 {
    flex: 1;
    margin: 0;
    font-size: 15px;
    font-weight: 650;
  }

  /* A segmented control, as in a macOS toolbar. */
  .segments {
    display: flex;
    padding: 2px;
    border-radius: 7px;
    background: var(--field);
  }

  .segments button {
    height: 24px;
    padding: 0 10px;
    border-radius: 5px;
    color: var(--text-2);
  }

  .segments button.on {
    color: var(--text);
    background: var(--window);
    box-shadow: 0 0 0 0.5px var(--hairline), 0 1px 2px rgba(0, 0, 0, 0.18);
  }

  .table {
    border-radius: 10px;
    background: var(--raised);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
    overflow-x: auto;
  }

  .tr {
    display: grid;
    grid-template-columns: minmax(140px, 1.6fr) repeat(8, minmax(72px, 1fr));
    gap: 10px;
    padding: 9px 14px;
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
  }

  .tr + .tr {
    border-top: 1px solid var(--hairline);
  }

  .tr span:not(:first-child) {
    text-align: right;
  }

  .tr.head {
    font-size: 11px;
    color: var(--text-3);
  }

  .tr .name {
    color: var(--text);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Availability worth a second look; the number itself says how bad. */
  .poor {
    color: var(--text);
    font-weight: 600;
  }
</style>
