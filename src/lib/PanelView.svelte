<script lang="ts">
  import { onMount } from "svelte";
  import { call } from "./api";
  import type { Device } from "./types";

  interface Field {
    name: string;
    label: string;
    type: "text" | "number";
    /** The column whose current value pre-fills this field. */
    from?: string;
  }
  interface Action {
    id: string;
    label: string;
    scope: "row" | "section";
    fields: Field[];
    /** Offered only when the row's cells match / do not match. */
    when?: Record<string, string>;
    unless?: Record<string, string>;
  }
  interface Row {
    id: string;
    cells: Record<string, string>;
  }
  interface Section {
    columns: { key: string; label: string; mono?: boolean }[];
    rows: Row[];
    actions: Action[];
  }
  interface Info {
    sections: { id: string; label: string }[];
    can_change: boolean;
    note: string;
  }
  interface Plan {
    title: string;
    commands: string[];
    warning: string;
  }

  let {
    device,
    query,
    onnotice,
    onback,
  }: {
    device: Device;
    query: string;
    onnotice: (text: string, bad?: boolean) => void;
    onback: () => void;
  } = $props();

  let info = $state<Info | null>(null);
  let problem = $state("");
  let tab = $state("");
  let data = $state<Section | null>(null);
  let loading = $state(false);
  let selected = $state<string | null>(null);

  // The change being made, from form to review to result.
  let sheet = $state<{
    action: Action;
    row: Row | null;
    values: Record<string, string>;
    plan: Plan | null;
    busy: boolean;
    error: string;
    output: string;
  } | null>(null);

  const GOOD = /^(running|connected|active|up|enabled|bound|yes)$/i;
  const BAD = /^(err-disabled|invalid|down|failed)$/i;
  const isState = (key: string) => ["state", "status", "protocol"].includes(key);

  const rows = $derived.by(() => {
    if (!data) return [];
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return data.rows;
    return data.rows.filter((row) => {
      const text = Object.values(row.cells).join(" ").toLowerCase();
      return words.every((word) => text.includes(word));
    });
  });

  const selectedRow = $derived(data?.rows.find((row) => row.id === selected) ?? null);

  function offered(action: Action, row: Row | null): boolean {
    if (action.scope === "section") return true;
    if (!row) return false;
    const matches = (rule: Record<string, string>) =>
      Object.entries(rule).every(([key, value]) => row.cells[key] === value);
    if (action.when && !matches(action.when)) return false;
    if (action.unless && matches(action.unless)) return false;
    return true;
  }

  const sectionActions = $derived(data?.actions.filter((a) => a.scope === "section") ?? []);
  const rowActions = $derived(data?.actions.filter((a) => a.scope === "row" && offered(a, selectedRow)) ?? []);

  async function open() {
    problem = "";
    info = null;
    try {
      info = await call<Info>("panel.open", { device: device.id });
      await show(info.sections[0].id);
    } catch (error) {
      problem = (error as Error).message;
    }
  }

  async function show(section: string) {
    tab = section;
    selected = null;
    loading = true;
    try {
      data = await call<Section>("panel.read", { device: device.id, section });
      problem = "";
    } catch (error) {
      data = null;
      problem = (error as Error).message;
    } finally {
      loading = false;
    }
  }

  function begin(action: Action) {
    const row = action.scope === "row" ? selectedRow : null;
    const values = Object.fromEntries(
      action.fields.map((field) => {
        const current = field.from && row ? row.cells[field.from] ?? "" : "";
        // A "trunk" VLAN cell is not a number to start editing from.
        return [field.name, field.type === "number" && !/^\d+$/.test(current) ? "" : current];
      }),
    );
    sheet = { action, row, values, plan: null, busy: false, error: "", output: "" };
    if (action.fields.length === 0) void review();
  }

  const request = () => ({
    device: device.id,
    section: tab,
    action: sheet!.action.id,
    row: sheet!.row?.id ?? null,
    values: sheet!.values,
  });

  async function review() {
    if (!sheet) return;
    sheet.busy = true;
    sheet.error = "";
    try {
      sheet.plan = await call<Plan>("panel.plan", request());
    } catch (error) {
      sheet.error = (error as Error).message;
    } finally {
      if (sheet) sheet.busy = false;
    }
  }

  async function apply() {
    if (!sheet?.plan) return;
    sheet.busy = true;
    sheet.error = "";
    try {
      const result = await call<{ ok: boolean; output: string }>("panel.apply", request());
      if (result.ok) {
        onnotice(`Done: ${sheet.plan.title}`);
        sheet = null;
        await show(tab);
        return;
      }
      sheet.error = "The device did not accept this. Nothing after the rejected line was sent.";
      sheet.output = result.output;
    } catch (error) {
      sheet.error = (error as Error).message;
    } finally {
      if (sheet) sheet.busy = false;
    }
  }

  onMount(() => {
    void open();
    // Leaving the panel ends the session; nothing stays signed in unseen.
    return () => void call("panel.close", { device: device.id }).catch(() => {});
  });
</script>

<svelte:window onkeydown={(event) => event.key === "Escape" && sheet && !sheet.busy && (sheet = null)} />

<div class="panel">
  <div class="bar">
    <button class="back" onclick={onback}>
      <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M6.5 1.5 3 5l3.5 3.5" /></svg>
      Devices
    </button>
    {#if info}
      <div class="segments" role="tablist">
        {#each info.sections as section (section.id)}
          <button role="tab" aria-selected={tab === section.id} class:on={tab === section.id}
                  onclick={() => show(section.id)}>{section.label}</button>
        {/each}
      </div>
    {/if}
  </div>

  {#if info?.note}<p class="note">{info.note}</p>{/if}

  {#if !info && !problem}
    <div class="blank"><h2>Signing in to {device.title}…</h2><p>Using the details stored in the vault.</p></div>
  {:else if problem}
    <div class="blank">
      <h2>Could not {info ? "read this section" : "open the device"}</h2>
      <p class="selectable">{problem}</p>
      <button class="button" onclick={() => (info ? show(tab) : open())}>Try again</button>
    </div>
  {:else if data}
    <div class="actions">
      <span class="count">
        {loading
          ? "Reading…"
          : `${rows.length} ${rows.length === 1 ? "row" : "rows"}${selectedRow && rowActions.length ? `, selected ${selectedRow.id}` : ""}`}
      </span>
      {#each rowActions as action (action.id)}
        <button class="button" onclick={() => begin(action)}>{action.label}</button>
      {/each}
      {#each sectionActions as action (action.id)}
        <button class="button" onclick={() => begin(action)}>{action.label}</button>
      {/each}
      <button class="button" disabled={loading} onclick={() => show(tab)}>Reload</button>
    </div>

    <div class="table" style:--columns={data.columns.length}>
      <div class="tr head">
        {#each data.columns as column}<span>{column.label}</span>{/each}
      </div>
      <div class="rows">
        {#each rows as row (row.id)}
          <button class="tr" class:selected={row.id === selected}
                  onclick={() => (selected = row.id === selected ? null : row.id)}>
            {#each data.columns as column}
              {@const value = row.cells[column.key] ?? ""}
              <span class:mono={column.mono} class="selectable">
                {#if isState(column.key) && value}
                  <i class:good={GOOD.test(value)} class:bad={BAD.test(value)}></i>
                {/if}{value || "—"}
              </span>
            {/each}
          </button>
        {:else}
          <p class="none">{query ? `Nothing here matches “${query}”.` : "The device returned nothing for this section."}</p>
        {/each}
      </div>
    </div>
  {/if}
</div>

{#if sheet}
  <div class="veil" role="presentation" onclick={(e) => e.target === e.currentTarget && !sheet?.busy && (sheet = null)}>
    <div class="sheet" role="dialog" aria-modal="true" aria-label={sheet.plan?.title ?? sheet.action.label}>
      {#if !sheet.plan}
        <h2>{sheet.action.label}{sheet.row ? `: ${sheet.row.id}` : ""}</h2>
        <form onsubmit={(event) => { event.preventDefault(); void review(); }}>
          {#each sheet.action.fields as field, index (field.name)}
            <label>
              <span>{field.label}</span>
              <!-- svelte-ignore a11y_autofocus -->
              <input type="text" inputmode={field.type === "number" ? "numeric" : "text"}
                     bind:value={sheet.values[field.name]} autofocus={index === 0} spellcheck="false" />
            </label>
          {/each}
          {#if sheet.error}<p class="error selectable">{sheet.error}</p>{/if}
          <div class="buttons">
            <button type="button" class="button" onclick={() => (sheet = null)}>Cancel</button>
            <button type="submit" class="button primary" disabled={sheet.busy}>
              {sheet.busy ? "Checking…" : "Review commands"}
            </button>
          </div>
        </form>
      {:else}
        <h2>{sheet.plan.title}</h2>
        <p class="lead">These commands will be sent to {device.title}, exactly as shown:</p>
        <pre class="selectable">{sheet.plan.commands.join("\n")}</pre>
        {#if sheet.plan.warning}<p class="warning">{sheet.plan.warning}</p>{/if}
        {#if sheet.error}
          <p class="error selectable">{sheet.error}</p>
          {#if sheet.output}<pre class="selectable output">{sheet.output}</pre>{/if}
        {/if}
        <div class="buttons">
          <button class="button" disabled={sheet.busy} onclick={() => (sheet = null)}>Cancel</button>
          {#if sheet.action.fields.length}
            <button class="button" disabled={sheet.busy}
                    onclick={() => { if (sheet) { sheet.plan = null; sheet.error = ""; sheet.output = ""; } }}>
              Back
            </button>
          {/if}
          <button class="button primary" disabled={sheet.busy} onclick={apply}>
            {sheet.busy ? "Sending…" : "Send to device"}
          </button>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .panel {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .bar,
  .actions {
    flex: none;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 20px 0;
  }

  .back {
    display: flex;
    align-items: center;
    gap: 5px;
    height: 26px;
    padding: 0 8px 0 6px;
    margin-left: -6px;
    border-radius: 6px;
    color: var(--accent);
  }

  .back:hover {
    background: var(--accent-soft);
  }

  .back svg {
    width: 10px;
    height: 10px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.7;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .segments {
    display: flex;
    padding: 2px;
    margin-left: 6px;
    border-radius: 7px;
    background: var(--field);
    overflow-x: auto;
  }

  .segments button {
    height: 24px;
    padding: 0 11px;
    border-radius: 5px;
    color: var(--text-2);
    white-space: nowrap;
  }

  .segments button.on {
    color: var(--text);
    background: var(--window);
    box-shadow: 0 0 0 0.5px var(--hairline), 0 1px 2px rgba(0, 0, 0, 0.18);
  }

  .note {
    flex: none;
    margin: 10px 20px 0;
    padding: 8px 12px;
    border-radius: 8px;
    line-height: 1.5;
    background: color-mix(in srgb, var(--orange) 12%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--orange) 30%, transparent);
  }

  .count {
    flex: 1;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .table {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    margin-top: 8px;
    overflow-x: auto;
  }

  .tr {
    display: grid;
    grid-template-columns: repeat(var(--columns), minmax(90px, 1fr));
    align-items: center;
    gap: 14px;
    padding: 0 12px;
    min-width: calc(var(--columns) * 104px);
  }

  .tr.head {
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

  button.tr {
    width: 100%;
    min-height: 32px;
    border-radius: 6px;
    text-align: left;
  }

  button.tr:nth-child(even) {
    background: var(--stripe);
  }

  .tr span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-2);
    cursor: default;
  }

  button.tr span:first-child {
    color: var(--text);
    font-weight: 500;
  }

  button.tr.selected {
    background: var(--accent);
  }

  button.tr.selected span {
    color: var(--on-accent);
  }

  i {
    display: inline-block;
    width: 7px;
    height: 7px;
    margin-right: 7px;
    border-radius: 50%;
    box-shadow: inset 0 0 0 1.5px var(--text-3);
  }

  i.good { background: var(--green); box-shadow: none; }
  i.bad { background: var(--red); box-shadow: none; }

  .selected i {
    box-shadow: inset 0 0 0 1.5px var(--on-accent);
  }

  .selected i.good,
  .selected i.bad {
    box-shadow: 0 0 0 1.5px var(--on-accent);
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
    max-width: 52ch;
    line-height: 1.55;
    color: var(--text-2);
  }

  .blank .button {
    margin-top: 10px;
  }

  /* --- the change sheet --- */
  .veil {
    position: fixed;
    inset: 0;
    z-index: 20;
    display: grid;
    place-items: start center;
    padding-top: 96px;
    border-radius: var(--radius-window);
    background: rgba(0, 0, 0, 0.32);
  }

  .sheet {
    width: min(520px, calc(100% - 48px));
    padding: 20px 22px 18px;
    border-radius: 12px;
    background: var(--window);
    box-shadow: 0 0 0 0.5px var(--edge), 0 18px 50px rgba(0, 0, 0, 0.45);
    animation: drop 0.16s ease-out;
  }

  @keyframes drop {
    from { opacity: 0; transform: translateY(-8px); }
  }

  .sheet h2 {
    margin: 0 0 12px;
    font-size: 15px;
    font-weight: 650;
  }

  .lead {
    margin: 0 0 8px;
    color: var(--text-2);
  }

  label {
    display: grid;
    grid-template-columns: 110px 1fr;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    color: var(--text-2);
  }

  input {
    height: 28px;
    padding: 0 9px;
    border: 0;
    border-radius: 6px;
    background: var(--field);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
    color: var(--text);
    user-select: text;
  }

  pre {
    margin: 0;
    padding: 11px 14px;
    border-radius: 8px;
    background: var(--code);
    box-shadow: inset 0 0 0 1px var(--hairline);
    font: 12.5px/1.75 var(--mono);
    font-feature-settings: "calt" 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 220px;
    overflow-y: auto;
  }

  .output {
    margin-top: 8px;
  }

  .warning,
  .error {
    margin: 12px 0 0;
    padding: 9px 12px;
    border-radius: 8px;
    line-height: 1.5;
  }

  .warning {
    background: color-mix(in srgb, var(--orange) 12%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--orange) 30%, transparent);
  }

  .error {
    box-shadow: inset 0 0 0 1px var(--red);
  }

  .buttons {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 18px;
  }
</style>
