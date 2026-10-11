<script lang="ts">
  import { copyText } from "./api";
  import { guideFor, searchGuide } from "./guides";
  import type { GuideItem, Platform } from "./types";

  let {
    platform,
    query = "",
    collapsible = false,
  }: {
    platform: Platform;
    query?: string;
    /** Start each command folded to its title, for narrow places. */
    collapsible?: boolean;
  } = $props();

  const guide = $derived(guideFor(platform));
  const groups = $derived(guide ? searchGuide(guide, query) : []);

  let open = $state<Record<string, boolean>>({});
  let copied = $state<string | null>(null);

  const isOpen = (item: GuideItem) => !collapsible || query.trim() !== "" || open[item.id];

  async function copy(key: string, lines: string[]) {
    if (await copyText(lines.join("\n"))) {
      copied = key;
      setTimeout(() => copied === key && (copied = null), 1600);
    }
  }

  /** Split a formula line so its <placeholders> can be coloured. */
  const parts = (line: string) => line.split(/(<[^<>]+>)/).filter(Boolean);
</script>

{#if !guide}
  <p class="empty" dir="rtl">برای این نوع دستگاه هنوز راهنمایی نوشته نشده است.</p>
{:else if groups.length === 0}
  <p class="empty" dir="rtl">دستوری با «{query}» پیدا نشد.</p>
{:else}
  <div class="guide" dir="rtl">
    {#each groups as group (group.title)}
      <section>
        <h3>{group.title}</h3>
        {#each group.items as item (item.id)}
          <article class:folded={!isOpen(item)}>
            {#if collapsible}
              <button class="heading" aria-expanded={isOpen(item)}
                      onclick={() => (open[item.id] = !open[item.id])}>
                <svg class="chevron" viewBox="0 0 10 10" aria-hidden="true"><path d="M6.5 1.5 3 5l3.5 3.5" /></svg>
                <span>{item.title}</span>
              </button>
            {:else}
              <h4 class="heading">{item.title}</h4>
            {/if}

            {#if isOpen(item)}
              <p class="summary selectable">{item.summary}</p>

              <div class="block">
                <header>
                  <span>فرمول</span>
                  {#if item.prompt}<code class="prompt" dir="ltr">{item.prompt}</code>{/if}
                </header>
                <pre class="selectable" dir="ltr">{#each item.syntax as line}{#each parts(line) as part}{#if part.startsWith("<")}<span class="ph">{part}</span>{:else}{part}{/if}{/each}{"\n"}{/each}</pre>
              </div>

              {#if item.params?.length}
                <dl>
                  {#each item.params as param}
                    <dt dir="ltr">{param.name}</dt>
                    <dd class="selectable">{param.desc}</dd>
                  {/each}
                </dl>
              {/if}

              <div class="block">
                <header>
                  <span>نمونه‌ی آماده</span>
                  <button class="copy" onclick={() => copy(item.id, item.example)}>
                    {copied === item.id ? "کپی شد" : "کپی"}
                  </button>
                </header>
                <pre class="selectable" dir="ltr">{item.example.join("\n")}</pre>
              </div>

              {#if item.note}
                <p class="note selectable">{item.note}</p>
              {/if}
            {/if}
          </article>
        {/each}
      </section>
    {/each}
  </div>
{/if}

<style>
  .guide {
    font-family: var(--fa);
  }

  .empty {
    font-family: var(--fa);
    color: var(--text-2);
    margin: 24px 0;
    text-align: center;
  }

  h3 {
    margin: 26px 0 10px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-2);
  }

  section:first-child h3 {
    margin-top: 0;
  }

  article {
    padding: 16px 0 18px;
    border-top: 1px solid var(--hairline);
  }

  article.folded {
    padding: 0;
  }

  .heading {
    margin: 0;
    font-size: 14.5px;
    font-weight: 650;
    line-height: 1.6;
  }

  button.heading {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 8px;
    text-align: right;
    font-family: inherit;
  }

  .folded button.heading {
    padding: 9px 0;
    font-weight: 500;
    font-size: 13.5px;
  }

  .chevron {
    width: 10px;
    height: 10px;
    flex: none;
    fill: none;
    stroke: var(--text-3);
    stroke-width: 1.6;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 0.15s;
  }

  [aria-expanded="true"] .chevron {
    transform: rotate(-90deg);
  }

  .summary {
    margin: 6px 0 14px;
    line-height: 2;
    color: var(--text-2);
  }

  .block {
    margin: 12px 0;
    border-radius: 8px;
    background: var(--code);
    box-shadow: inset 0 0 0 1px var(--hairline);
    overflow: hidden;
  }

  .block header {
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 12px;
    font-size: 11.5px;
    color: var(--text-2);
    border-bottom: 1px solid var(--hairline);
  }

  .prompt {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-3);
  }

  pre {
    margin: 0;
    padding: 11px 14px;
    font: 12.5px/1.75 var(--mono);
    font-feature-settings: "calt" 0;
    text-align: left;
    white-space: pre;
    overflow-x: auto;
    tab-size: 2;
  }

  /* The parts you replace with your own values. */
  .ph {
    color: var(--placeholder);
    font-style: italic;
  }

  .copy {
    font-family: inherit;
    font-size: 11.5px;
    color: var(--accent);
    padding: 3px 6px;
    margin: 0 -6px;
    border-radius: 5px;
  }

  .copy:hover {
    background: var(--accent-soft);
  }

  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 8px 16px;
    margin: 14px 0;
    align-items: baseline;
  }

  dt {
    font: 12px/1.9 var(--mono);
    color: var(--placeholder);
    text-align: left;
    max-width: 22ch;
  }

  dd {
    margin: 0;
    line-height: 1.9;
    color: var(--text-2);
  }

  .note {
    margin: 14px 0 0;
    padding: 10px 14px;
    border-radius: 8px;
    line-height: 1.95;
    background: color-mix(in srgb, var(--orange) 11%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--orange) 28%, transparent);
  }
</style>
