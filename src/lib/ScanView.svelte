<script lang="ts">
  import { onMount } from "svelte";
  import { scan } from "./scan.svelte";
  import { PLATFORM_LABELS, type Device, type ScanHost } from "./types";

  let {
    devices,
    query,
    adding,
    onadd,
  }: {
    devices: Device[];
    query: string;
    /** Address of the host being saved right now, if any. */
    adding: string | null;
    onadd: (host: ScanHost) => void;
  } = $props();

  const SERVICES: Record<number, string> = {
    22: "SSH", 23: "Telnet", 80: "HTTP", 443: "HTTPS", 445: "SMB", 3389: "RDP", 8291: "Winbox",
  };

  const known = $derived(new Map(devices.map((device) => [device.address, device])));

  const visible = $derived.by(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    return scan.sorted.filter((host) => {
      const text = [host.address, host.hostname, host.mac, ...host.ports.map((p) => SERVICES[p])]
        .join(" ")
        .toLowerCase();
      return words.every((word) => text.includes(word));
    });
  });

  const fresh = $derived(scan.sorted.filter((host) => !known.has(host.address)).length);

  onMount(() => void scan.loadNetworks());
</script>

<div class="controls">
  <label>
    <span>Range</span>
    <input class="mono" list="networks" bind:value={scan.range} placeholder="192.168.1.0/24"
           disabled={scan.running} spellcheck="false"
           onkeydown={(event) => event.key === "Enter" && scan.start()} />
    <datalist id="networks">
      {#each scan.networks as network (network.interface + network.scan)}
        <option value={network.scan}>{network.interface}</option>
      {/each}
    </datalist>
  </label>
  {#if scan.running}
    <button class="button" onclick={() => scan.stop()}>Stop</button>
  {:else}
    <button class="button primary" disabled={!scan.range.trim()} onclick={() => scan.start()}>Scan</button>
  {/if}
  <span class="summary">
    {#if scan.running}
      Checked {scan.done} of {scan.total} addresses
    {:else if scan.scanned}
      {scan.sorted.length} found in {scan.scanned}{fresh ? `, ${fresh} not in the vault` : ""}
    {/if}
  </span>
</div>

<div class="progress" class:active={scan.running}>
  <div style:width={`${scan.total ? (scan.done / scan.total) * 100 : 0}%`}></div>
</div>

{#if scan.problem}
  <p class="problem selectable">{scan.problem}</p>
{/if}

{#if !scan.scanned && !scan.running}
  <div class="blank">
    <h2>Find what is on the network</h2>
    <p>A scan pings every address in the range, then checks which of the usual management ports each answering host has open. Hosts that are not in your vault yet can be added with one click.</p>
    <p>It needs no administrator rights and covers up to 1024 addresses at a time.</p>
  </div>
{:else}
  <div class="table" role="grid" aria-label="Hosts found">
    <div class="head" role="row">
      <span role="columnheader">Address</span>
      <span role="columnheader">Name</span>
      <span role="columnheader">MAC</span>
      <span role="columnheader">Open services</span>
      <span role="columnheader">In the vault</span>
    </div>
    <div class="rows">
      {#each visible as host (host.address)}
        {@const device = known.get(host.address)}
        <div class="line" role="row">
          <span class="address mono selectable" role="gridcell">
            <i class:quiet={host.ms === null} title={host.ms === null ? "Does not answer ping" : `${host.ms} ms`}></i>
            {host.address}
          </span>
          <span class="selectable" role="gridcell">{host.hostname || "—"}</span>
          <span class="mono selectable" role="gridcell">{host.mac || "—"}</span>
          <span class="services" role="gridcell">
            {#each host.ports as port}<em>{SERVICES[port] ?? port}</em>{/each}
            {#if host.platform}<strong>{PLATFORM_LABELS[host.platform]}?</strong>{/if}
            {#if !host.ports.length}—{/if}
          </span>
          <span role="gridcell">
            {#if device}
              <span class="have">{device.title}</span>
            {:else}
              <button class="add" disabled={adding !== null} onclick={() => onadd(host)}>
                {adding === host.address ? "Adding…" : "Add to vault"}
              </button>
            {/if}
          </span>
        </div>
      {:else}
        <p class="none">
          {#if scan.running}Looking…{:else if query}Nothing found matches “{query}”.{:else}Nothing answered in this range.{/if}
        </p>
      {/each}
    </div>
  </div>
{/if}

<style>
  .controls {
    flex: none;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 20px 10px;
  }

  label {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-2);
  }

  input {
    width: 170px;
    height: 28px;
    padding: 0 9px;
    border: 0;
    border-radius: 6px;
    background: var(--field);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
    color: var(--text);
    user-select: text;
  }

  input:disabled {
    opacity: 0.6;
  }

  .summary {
    margin-left: 4px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }

  .progress {
    flex: none;
    height: 2px;
    margin: 0 20px;
    border-radius: 1px;
    background: var(--hairline);
    opacity: 0;
    transition: opacity 0.3s 0.4s;
  }

  .progress.active {
    opacity: 1;
    transition-delay: 0s;
  }

  .progress div {
    height: 100%;
    border-radius: 1px;
    background: var(--accent);
    transition: width 0.25s linear;
  }

  .problem {
    margin: 10px 20px 0;
    padding: 9px 12px;
    border-radius: 8px;
    box-shadow: inset 0 0 0 1px var(--red);
    line-height: 1.5;
  }

  .table {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    margin-top: 6px;
  }

  .head,
  .line {
    display: grid;
    grid-template-columns: minmax(130px, 1fr) minmax(120px, 1.6fr) minmax(140px, 1.1fr) minmax(150px, 1.6fr) minmax(120px, 1.1fr);
    align-items: center;
    gap: 14px;
    padding: 0 12px;
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
    min-height: 34px;
    border-radius: 6px;
  }

  .line:nth-child(even) {
    background: var(--stripe);
  }

  .line > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-2);
  }

  .line .address {
    display: flex;
    align-items: center;
    gap: 9px;
    color: var(--text);
  }

  i {
    width: 8px;
    height: 8px;
    flex: none;
    border-radius: 50%;
    background: var(--green);
  }

  /* Seen on the wire but ignoring ping. */
  i.quiet {
    background: none;
    box-shadow: inset 0 0 0 1.5px var(--text-3);
  }

  .services {
    display: flex;
    align-items: center;
    gap: 5px;
  }

  em,
  strong {
    font-style: normal;
    font-size: 11px;
    font-weight: 500;
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--field);
    color: var(--text-2);
  }

  strong {
    background: var(--accent-soft);
    color: var(--accent);
  }

  .have {
    color: var(--text-2);
  }

  .add {
    color: var(--accent);
    font-weight: 500;
    padding: 3px 8px;
    margin-left: -8px;
    border-radius: 5px;
  }

  .add:hover:not(:disabled) {
    background: var(--accent-soft);
  }

  .add:disabled {
    opacity: 0.5;
  }

  .none {
    margin: 40px 0;
    text-align: center;
    color: var(--text-2);
  }

  .blank {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 0 40px 80px;
    text-align: center;
  }

  .blank h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 650;
  }

  .blank p {
    margin: 0;
    max-width: 50ch;
    line-height: 1.55;
    color: var(--text-2);
  }
</style>
