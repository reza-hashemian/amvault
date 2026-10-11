<script lang="ts">
  // A small time series: one or two lines over a shared time axis, with a
  // crosshair that reads out the values under the pointer.
  import { clock } from "./format";

  interface Series {
    label: string;
    /** A CSS colour, normally one of the --series tokens. */
    color: string;
    values: (number | null)[];
  }

  let {
    times,
    series,
    format,
    max,
    marks = [],
    markLabel = "",
    height = 56,
  }: {
    times: number[];
    series: Series[];
    format: (value: number | null) => string;
    /** A fixed top for the scale (100 for percentages); otherwise it fits the data. */
    max?: number;
    /** Indexes to flag on the baseline, e.g. pings that got no reply. */
    marks?: number[];
    markLabel?: string;
    height?: number;
  } = $props();

  const PAD = 3; // room for the line's own thickness at the edges

  let width = $state(300);
  let hover = $state<number | null>(null);

  const top = $derived.by(() => {
    if (max != null) return max;
    let peak = 0;
    for (const s of series) for (const v of s.values) if (v != null && v > peak) peak = v;
    return peak > 0 ? peak * 1.15 : 1;
  });

  const x = (index: number) =>
    times.length < 2 ? width - PAD : PAD + (index / (times.length - 1)) * (width - 2 * PAD);
  const y = (value: number) => height - PAD - (Math.min(value, top) / top) * (height - 2 * PAD);

  /** One path per series, broken wherever a reading is missing. */
  const paths = $derived(
    series.map((s) => {
      let d = "";
      let pen = false;
      s.values.forEach((value, index) => {
        if (value == null) {
          pen = false;
          return;
        }
        d += `${pen ? "L" : "M"}${x(index).toFixed(1)} ${y(value).toFixed(1)}`;
        pen = true;
      });
      return d;
    }),
  );

  /** The last real point of each series, for the end marker. */
  const ends = $derived(
    series.map((s) => {
      for (let index = s.values.length - 1; index >= 0; index--) {
        const value = s.values[index];
        if (value != null) return { index, value };
      }
      return null;
    }),
  );

  function move(event: PointerEvent) {
    if (times.length < 2) return;
    const box = (event.currentTarget as SVGElement).getBoundingClientRect();
    const ratio = (event.clientX - box.left - PAD) / (box.width - 2 * PAD);
    hover = Math.max(0, Math.min(times.length - 1, Math.round(ratio * (times.length - 1))));
  }

  /** The nearest real reading at or before an index: metrics are not read every tick. */
  function at(values: (number | null)[], index: number): number | null {
    for (let i = index; i >= 0 && i > index - 4; i--) if (values[i] != null) return values[i];
    return null;
  }
</script>

<div class="chart" bind:clientWidth={width}>
  <svg {width} {height} role="img"
       aria-label={series.map((s) => `${s.label}: ${format(ends[series.indexOf(s)]?.value ?? null)}`).join(", ")}
       onpointermove={move} onpointerleave={() => (hover = null)}>
    <line class="grid" x1="0" x2={width} y1={PAD} y2={PAD} />
    <line class="base" x1="0" x2={width} y1={height - 0.5} y2={height - 0.5} />

    {#each marks as index}
      <rect class="mark" x={x(index) - 1.5} y={height - 9} width="3" height="9" rx="1" />
    {/each}

    {#each paths as d, i}
      <path {d} stroke={series[i].color} />
      {#if ends[i]}
        <circle class="end" cx={x(ends[i].index)} cy={y(ends[i].value)} r="3" fill={series[i].color} />
      {/if}
    {/each}

    {#if hover != null}
      <line class="cross" x1={x(hover)} x2={x(hover)} y1="0" y2={height} />
      {#each series as s}
        {@const value = at(s.values, hover)}
        {#if value != null}
          <circle class="dot" cx={x(hover)} cy={y(value)} r="3.5" fill={s.color} />
        {/if}
      {/each}
    {/if}
  </svg>

  <span class="scale">{format(top)}</span>

  {#if hover != null}
    <div class="tip" class:flip={x(hover) > width / 2} style:left={`${x(hover)}px`}>
      <div class="when">{clock(times[hover])}</div>
      {#each series as s}
        <div class="row">
          {#if series.length > 1}<i style:background={s.color}></i><span>{s.label}</span>{/if}
          <b>{format(at(s.values, hover))}</b>
        </div>
      {/each}
      {#if marks.includes(hover)}<div class="row lost">{markLabel}</div>{/if}
    </div>
  {/if}
</div>

<style>
  .chart {
    position: relative;
  }

  svg {
    display: block;
    overflow: visible;
    touch-action: none;
  }

  path {
    fill: none;
    stroke-width: 2;
    stroke-linejoin: round;
    stroke-linecap: round;
  }

  .grid,
  .base {
    stroke: var(--hairline);
    stroke-width: 1;
  }

  .grid {
    stroke-dasharray: 2 3;
  }

  .end,
  .dot {
    stroke: var(--raised);
    stroke-width: 2;
  }

  .cross {
    stroke: var(--text-3);
    stroke-width: 1;
  }

  .mark {
    fill: var(--red);
  }

  .scale {
    position: absolute;
    top: -3px;
    right: 0;
    padding-left: 4px;
    font-size: 10px;
    line-height: 12px;
    color: var(--text-3);
    background: var(--raised);
    font-variant-numeric: tabular-nums;
    pointer-events: none;
  }

  .tip {
    position: absolute;
    bottom: calc(100% + 4px);
    z-index: 5;
    transform: translateX(8px);
    padding: 6px 9px;
    border-radius: 7px;
    background: var(--window);
    box-shadow: 0 0 0 0.5px var(--edge), 0 6px 20px rgba(0, 0, 0, 0.3);
    font-size: 11.5px;
    white-space: nowrap;
    pointer-events: none;
  }

  .tip.flip {
    transform: translateX(calc(-100% - 8px));
  }

  .when {
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 2px;
    color: var(--text-2);
  }

  .row b {
    margin-left: auto;
    padding-left: 8px;
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }

  .row i {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  .row.lost {
    color: var(--text);
  }
</style>
