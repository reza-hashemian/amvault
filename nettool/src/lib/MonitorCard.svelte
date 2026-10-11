<script lang="ts">
  import DeviceIcon from "./DeviceIcon.svelte";
  import { bytes, duration, millis, percent, rate } from "./format";
  import LineChart from "./LineChart.svelte";
  import { PLATFORM_LABELS, type Device, type Iface, type Sample } from "./types";

  let { device, samples, ifaces }: { device: Device; samples: Sample[]; ifaces: Iface[] } = $props();

  let showPorts = $state(false);

  const times = $derived(samples.map((s) => s.t));
  const last = $derived(samples.at(-1));

  /** The newest reading that actually carries a given metric. */
  function latest<K extends keyof Sample>(key: K): Sample | undefined {
    for (let i = samples.length - 1; i >= 0; i--) if (samples[i][key] != null) return samples[i];
    return undefined;
  }

  const cpu = $derived(latest("cpu"));
  const mem = $derived(latest("mem_used"));
  const traffic = $derived(latest("rx_bps"));
  const uptime = $derived(latest("uptime")?.uptime);
  const hasMetrics = $derived(Boolean(cpu || mem || traffic));

  const lost = $derived(samples.flatMap((s, i) => (s.up ? [] : [i])));
  const loss = $derived(samples.length ? (100 * lost.length) / samples.length : 0);
  const answered = $derived(samples.filter((s) => s.ms != null).map((s) => s.ms as number));
  const average = $derived(answered.length ? answered.reduce((a, b) => a + b, 0) / answered.length : null);

  const memPercent = (s: Sample) => (s.mem_used != null && s.mem_total ? (100 * s.mem_used) / s.mem_total : null);

  const upPorts = $derived(ifaces.filter((i) => i.up).length);
  // Busiest first, so the ports doing something are at the top.
  const sortedPorts = $derived(
    [...ifaces].sort((a, b) => (b.rx_bps ?? 0) + (b.tx_bps ?? 0) - ((a.rx_bps ?? 0) + (a.tx_bps ?? 0))),
  );
</script>

<article class="card">
  <header>
    <i class="state" class:up={last?.up} class:down={last && !last.up}></i>
    <DeviceIcon {device} />
    <h3>{device.title}</h3>
    <span class="meta mono">{device.address}</span>
    <span class="meta right">
      {PLATFORM_LABELS[device.platform]}{uptime != null ? `, up ${duration(uptime)}` : ""}
    </span>
  </header>

  <div class="stats" class:solo={!hasMetrics}>
    <section>
      <div class="figure">
        <span>Ping</span>
        <b>{last ? (last.up ? millis(last.ms) : "No reply") : "…"}</b>
        <small>avg {millis(average)}, {lost.length ? `${lost.length} lost (${percent(loss, 1)})` : "none lost"}</small>
      </div>
      <LineChart {times} format={millis} marks={lost} markLabel="No reply"
                 series={[{ label: "Ping", color: "var(--series-1)", values: samples.map((s) => s.ms) }]} />
    </section>

    {#if hasMetrics}
      <section>
        <div class="figure">
          <span>CPU</span>
          <b>{percent(cpu?.cpu)}</b>
        </div>
        <LineChart {times} format={(v) => percent(v)} max={100}
                   series={[{ label: "CPU", color: "var(--series-1)", values: samples.map((s) => s.cpu) }]} />
      </section>

      <section>
        <div class="figure">
          <span>Memory</span>
          <b>{percent(mem ? memPercent(mem) : null)}</b>
          <small>{mem ? `${bytes(mem.mem_used)} of ${bytes(mem.mem_total)}` : ""}</small>
        </div>
        <LineChart {times} format={(v) => percent(v)} max={100}
                   series={[{ label: "Memory", color: "var(--series-1)", values: samples.map(memPercent) }]} />
      </section>

      <section>
        <div class="figure">
          <span>Traffic</span>
          <small class="legend">
            <i style:background="var(--series-1)"></i>In <b>{rate(traffic?.rx_bps)}</b>
            <i style:background="var(--series-2)"></i>Out <b>{rate(traffic?.tx_bps)}</b>
          </small>
        </div>
        <LineChart {times} format={rate}
                   series={[
                     { label: "In", color: "var(--series-1)", values: samples.map((s) => s.rx_bps) },
                     { label: "Out", color: "var(--series-2)", values: samples.map((s) => s.tx_bps) },
                   ]} />
      </section>
    {/if}
  </div>

  {#if last?.note}
    <p class="note selectable">{last.note}</p>
  {/if}

  {#if ifaces.length}
    <button class="ports-toggle" aria-expanded={showPorts} onclick={() => (showPorts = !showPorts)}>
      <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M3.5 1.5 7 5 3.5 8.5" /></svg>
      {ifaces.length} ports, {upPorts} up
    </button>
    {#if showPorts}
      <div class="ports">
        <div class="port head"><span>Port</span><span>State</span><span>In</span><span>Out</span></div>
        {#each sortedPorts as port (port.name)}
          <div class="port">
            <span class="mono">{port.name}</span>
            <span class:off={!port.up}>{port.up ? "Up" : "Down"}</span>
            <span>{rate(port.rx_bps)}</span>
            <span>{rate(port.tx_bps)}</span>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</article>

<style>
  .card {
    padding: 14px 16px 12px;
    border-radius: 12px;
    background: var(--raised);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
  }

  header {
    display: flex;
    align-items: center;
    gap: 9px;
    min-width: 0;
    color: var(--text-2);
  }

  h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 650;
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    white-space: nowrap;
  }

  .meta.right {
    margin-left: auto;
    padding-left: 8px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .state {
    width: 9px;
    height: 9px;
    flex: none;
    border-radius: 50%;
    box-shadow: inset 0 0 0 1.5px var(--text-3);
  }

  .state.up { background: var(--green); box-shadow: none; }
  .state.down { background: var(--red); box-shadow: none; }

  .stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px 22px;
    margin-top: 16px;
  }

  .stats.solo {
    grid-template-columns: 1fr;
  }

  section {
    min-width: 0;
  }

  .figure {
    display: flex;
    align-items: baseline;
    gap: 8px;
    height: 22px;
    margin-bottom: 8px;
    white-space: nowrap;
  }

  .figure > span {
    font-size: 12px;
    color: var(--text-2);
  }

  .figure > b {
    font-size: 16px;
    font-weight: 650;
    letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums;
  }

  small {
    margin-left: auto;
    font-size: 11px;
    color: var(--text-3);
    overflow: hidden;
    text-overflow: ellipsis;
    font-variant-numeric: tabular-nums;
  }

  .legend {
    display: flex;
    align-items: center;
    gap: 5px;
    color: var(--text-2);
  }

  .legend b {
    margin-right: 6px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text);
  }

  .legend i {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  .note {
    margin: 12px 0 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-2);
  }

  .ports-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 12px;
    padding: 3px 6px;
    margin-left: -6px;
    border-radius: 5px;
    font-size: 12px;
    color: var(--text-2);
  }

  .ports-toggle:hover {
    background: var(--hover);
  }

  .ports-toggle svg {
    width: 9px;
    height: 9px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 0.15s;
  }

  .ports-toggle[aria-expanded="true"] svg {
    transform: rotate(90deg);
  }

  .ports {
    margin-top: 6px;
    max-height: 240px;
    overflow-y: auto;
  }

  .port {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 54px 84px 84px;
    gap: 10px;
    padding: 5px 0;
    font-size: 12px;
    border-top: 1px solid var(--hairline);
    font-variant-numeric: tabular-nums;
  }

  .port span:nth-child(n + 3) {
    text-align: right;
  }

  .port span:first-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .port.head {
    border-top: 0;
    color: var(--text-3);
    font-size: 11px;
  }

  .off {
    color: var(--text-3);
  }
</style>
