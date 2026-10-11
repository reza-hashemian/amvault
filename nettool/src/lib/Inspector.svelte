<script lang="ts">
  import DeviceIcon from "./DeviceIcon.svelte";
  import GuideList from "./GuideList.svelte";
  import { PLATFORM_LABELS, type Device, type Reach, type Status } from "./types";

  let {
    device,
    status,
    reach,
    busy,
    onssh,
    onmanage,
    onremote,
    onclose,
  }: {
    device: Device;
    status: Status | null;
    /** The last ping, if the device has been checked. */
    reach: Reach | undefined;
    busy: boolean;
    onssh: (device: Device) => void;
    onmanage: (device: Device) => void;
    onremote: (device: Device, kind: "rdp" | "rustdesk") => void;
    onclose: () => void;
  } = $props();

  // Platforms that have a management panel.
  const MANAGEABLE = ["mikrotik", "cisco_ios", "linux", "windows"];
  const REMOTE_LABELS = { rdp: "Remote Desktop (RDP)", rustdesk: "RustDesk" };

  let guideQuery = $state("");

  const FIELD_LABELS: [string, string][] = [
    ["mgmt_ip", "Address"],
    ["host", "Host"],
    ["mgmt_port", "Port"],
    ["port", "Port"],
    ["username", "User"],
    ["vendor", "Vendor"],
    ["model", "Model"],
    ["device_type", "Type"],
    ["firmware", "Firmware"],
    ["location", "Location"],
    ["wifi_ssid", "Wi-Fi name"],
    ["rustdesk_id", "RustDesk ID"],
    ["key_type", "Key type"],
    ["fingerprint", "Fingerprint"],
  ];

  const rows = $derived(
    FIELD_LABELS.filter(([name]) => device.fields[name]).map(([name, label]) => ({
      label,
      value: device.fields[name],
      mono: /ip|host|port|fingerprint|_id/.test(name),
    })),
  );

  const subtitle = $derived(
    [device.fields.vendor, device.fields.model].filter(Boolean).join(" ") ||
      PLATFORM_LABELS[device.platform] + (device.platform_detected ? ", recognised from its SSH greeting" : ""),
  );

  const signIn = $derived(
    device.has_key ? "SSH key from the vault" : device.has_password ? "Password from the vault" : "Nothing stored",
  );

  // Why the button is unavailable, in the place the person is looking.
  const blocked = $derived.by(() => {
    if (!device.address) {
      return device.remote?.includes("rustdesk")
        ? "No address on this entry, so only RustDesk is available."
        : "This entry has no address.";
    }
    if (status?.source === "vault" && status.locked) return "Unlock amvault to connect.";
    if (status?.source === "disconnected") return "amvault was closed. Reopen this tool from amvault.";
    return "";
  });
</script>

<aside class="inspector">
  <header data-tauri-drag-region>
    <button class="close" aria-label="Close details" onclick={onclose}>
      <svg viewBox="0 0 10 10"><path d="M2 2l6 6M8 2 2 8" /></svg>
    </button>
  </header>

  <div class="scroll">
    <div class="identity">
      <div class="tile"><DeviceIcon {device} size={26} /></div>
      <div class="names">
        <h2 class="selectable">{device.title}</h2>
        <p>{subtitle}</p>
      </div>
    </div>

    <div class="actions">
      <button class="button primary" disabled={busy || blocked !== ""} onclick={() => onssh(device)}>
        {busy ? "Opening…" : "Open SSH in Terminal"}
      </button>
      {#if MANAGEABLE.includes(device.platform)}
        <button class="button" disabled={blocked !== ""} onclick={() => onmanage(device)}>Manage</button>
      {/if}
    </div>
    {#if device.remote?.length}
      <div class="actions remote">
        {#each device.remote as kind}
          <button class="button" disabled={kind === "rdp" && blocked !== ""}
                  onclick={() => onremote(device, kind)}>{REMOTE_LABELS[kind]}</button>
        {/each}
      </div>
    {/if}
    {#if blocked}<p class="hint">{blocked}</p>{/if}

    <div class="group">
      {#if reach}
        <div class="field">
          <span>Status</span>
          <span class="value">
            <i class:up={reach.up}></i>{reach.up ? `Online, ${reach.ms} ms` : "Not answering ping"}
          </span>
        </div>
      {/if}
      {#each rows as row}
        <div class="field">
          <span>{row.label}</span>
          <span class="value selectable" class:mono={row.mono}>{row.value}</span>
        </div>
      {/each}
      <div class="field">
        <span>Sign-in</span>
        <span class="value">{signIn}</span>
      </div>
    </div>

    {#if device.platform !== "unknown"}
      <div class="guide-head">
        <h3>{PLATFORM_LABELS[device.platform]} commands</h3>
        <input type="search" placeholder="Search" bind:value={guideQuery} />
      </div>
      <GuideList platform={device.platform} query={guideQuery} collapsible />
    {:else}
      <p class="hint">
        This device did not say what it is (its SSH port gave no answer). Add a vendor to its entry in amvault (Cisco, MikroTik, Linux or Windows) to get its command guide, panel and monitoring.
      </p>
    {/if}
  </div>
</aside>

<style>
  .inspector {
    width: 400px;
    flex: none;
    display: flex;
    flex-direction: column;
    border-left: 1px solid var(--hairline);
    background: var(--window);
  }

  header {
    height: 52px;
    flex: none;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding: 0 14px;
  }

  .close {
    width: 24px;
    height: 24px;
    border-radius: 6px;
    display: grid;
    place-items: center;
  }

  .close:hover {
    background: var(--hover);
  }

  .close svg {
    width: 10px;
    height: 10px;
    stroke: var(--text-2);
    stroke-width: 1.4;
    stroke-linecap: round;
  }

  .scroll {
    flex: 1;
    overflow-y: auto;
    padding: 0 20px 28px;
  }

  .identity {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .tile {
    width: 52px;
    height: 52px;
    flex: none;
    border-radius: 12px;
    display: grid;
    place-items: center;
    color: var(--accent);
    background: var(--accent-soft);
  }

  .names {
    min-width: 0;
  }

  h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 650;
    letter-spacing: -0.01em;
    line-height: 1.25;
  }

  .names p {
    margin: 2px 0 0;
    color: var(--text-2);
  }

  .actions {
    display: flex;
    gap: 8px;
    margin-top: 18px;
  }

  .actions .button {
    height: 30px;
  }

  .actions .button.primary,
  .actions.remote .button {
    flex: 1;
  }

  .actions.remote {
    margin-top: 8px;
  }

  .hint {
    margin: 10px 0 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-2);
  }

  /* A grouped form, as in System Settings. */
  .group {
    margin-top: 20px;
    border-radius: 10px;
    background: var(--raised);
    box-shadow: inset 0 0 0 0.5px var(--hairline);
  }

  .field {
    min-height: 36px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 6px 14px;
  }

  .field + .field {
    border-top: 1px solid var(--hairline);
  }

  .value {
    color: var(--text-2);
    text-align: right;
    overflow-wrap: anywhere;
  }

  .value i {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 7px;
    border-radius: 50%;
    background: var(--red);
  }

  .value i.up {
    background: var(--green);
  }

  .guide-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 30px 0 10px;
  }

  h3 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
  }

  input {
    width: 130px;
    height: 24px;
    padding: 0 8px;
    border: 0;
    border-radius: 6px;
    background: var(--field);
    user-select: text;
  }
</style>
