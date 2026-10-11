<script lang="ts">
  import type { Device } from "./types";

  let { device, size = 16 }: { device: Device; size?: number } = $props();

  // Drawn from what the device does, falling back to what it runs.
  const kind = $derived.by(() => {
    // The type field when it is filled in; otherwise the name and model
    // usually say it ("Floor switch", "Catalyst 2960X").
    const type = (
      device.fields.device_type || `${device.title} ${device.fields.model ?? ""}`
    ).toLowerCase();
    if (/fire|utm/.test(type)) return "firewall";
    if (/access point|wifi|wireless|\bap\b/.test(type)) return "wifi";
    if (/switch|catalyst|\bcrs\d/.test(type)) return "switch";
    if (/router|gateway/.test(type)) return "router";
    if (/server|host|vm/.test(type)) return "server";
    if (device.platform === "linux" || device.platform === "windows") return "server";
    if (device.category === "ssh_key") return "server";
    return "router";
  });
</script>

<svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor"
     stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  {#if kind === "switch"}
    <rect x="1.5" y="5" width="13" height="6" rx="1.5" />
    <path d="M4 8h.01M6.5 8h.01M9 8h.01M11.5 8h.01" stroke-width="1.6" />
  {:else if kind === "server"}
    <rect x="2.5" y="2" width="11" height="5" rx="1.3" />
    <rect x="2.5" y="9" width="11" height="5" rx="1.3" />
    <path d="M5 4.5h.01M5 11.5h.01" stroke-width="1.6" />
  {:else if kind === "wifi"}
    <path d="M2 6.5a8.5 8.5 0 0 1 12 0M4.2 8.8a5.4 5.4 0 0 1 7.6 0M6.3 11a2.4 2.4 0 0 1 3.4 0" />
    <path d="M8 13.2h.01" stroke-width="1.8" />
  {:else if kind === "firewall"}
    <path d="M8 1.8 13 3.6v4.1c0 3-2.1 5.3-5 6.5-2.9-1.2-5-3.5-5-6.5V3.6z" />
    <path d="M6 8l1.5 1.5L10.2 6.6" />
  {:else}
    <rect x="1.5" y="8.5" width="13" height="5" rx="1.5" />
    <path d="M4.2 11h.01M6.6 11h.01" stroke-width="1.6" />
    <path d="M11 8.5V4M8.4 5.6a3.6 3.6 0 0 1 5.2 0" />
  {/if}
</svg>
