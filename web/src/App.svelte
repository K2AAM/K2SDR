<script lang="ts">
  import { Connection, type ConnStatus } from "./lib/client";
  import { formatFrequency } from "./lib/freq";
  import SpectrumView from "./SpectrumView.svelte";
  import type { Json } from "./lib/patch";

  type Receiver = {
    label: string; driver: string; capture_gen: number;
    acquisition: { state: string; detail: string | null };
    capture: { center_hz: { applied: number | null }; sample_rate_hz: { applied: number | null } };
  };

  const conn = new Connection();
  let status: ConnStatus = $state("connecting");
  let receivers: Record<string, Receiver> = $state({});
  let selected: string | null = $state(null);

  conn.onStatus((s) => (status = s));
  conn.onState(() => {
    const st = conn.state as { receivers?: Record<string, Json> } | null;
    receivers = (st?.receivers ?? {}) as unknown as Record<string, Receiver>;
    if (selected === null || !(selected in receivers)) selected = Object.keys(receivers)[0] ?? null;
  });
  conn.connect();

  const rx = $derived(selected !== null ? receivers[selected] : undefined);
  const statusText: Record<ConnStatus, string> = {
    connecting: "connecting…", open: "connected", reconnecting: "reconnecting…", signed_out: "signed out",
  };
</script>

<header>
  <strong class="brand">K2AAM WebSDR</strong>
  <select aria-label="Receiver" bind:value={selected} disabled={Object.keys(receivers).length < 2}>
    {#each Object.entries(receivers) as [id, r] (id)}
      <option value={id}>{r.label}</option>
    {/each}
  </select>
  <span class="conn {status}" role="status">{statusText[status]}</span>
  <a class="status-link" href="/status">Status</a>
</header>

<main>
  {#if rx && selected}
    <section class="rx">
      <span>{rx.label} <span class="muted">({rx.driver})</span></span>
      <span class="acq {rx.acquisition.state}">
        {rx.acquisition.state}{rx.acquisition.detail ? ` — ${rx.acquisition.detail}` : ""}
      </span>
      <span>Centre {rx.capture.center_hz.applied !== null ? formatFrequency(rx.capture.center_hz.applied) : "—"}
        · {rx.capture.sample_rate_hz.applied ?? "—"} S/s · generation {rx.capture_gen}</span>
    </section>
    <div class="spectrum">
      {#key selected}
        <SpectrumView {conn} receiverId={selected} />
      {/key}
    </div>
  {:else}
    <p class="muted pad">No receivers configured.</p>
  {/if}
</main>

<style>
  header { display: flex; align-items: center; gap: .75rem; padding: .4rem .75rem; background: var(--panel);
           border-bottom: 1px solid var(--line); }
  .brand { font-weight: 600; }
  .conn { margin-left: auto; color: var(--muted); }
  .conn.open { color: var(--ok); }
  .conn.reconnecting { color: var(--warn); }
  .status-link { color: var(--accent); }
  :global(#app) { display: grid; grid-template-rows: auto 1fr; }
  main { display: grid; grid-template-rows: auto 1fr; min-height: 0; }
  .rx { display: flex; flex-wrap: wrap; gap: .3rem 1rem; padding: .3rem .75rem; font-size: 13px;
        border-bottom: 1px solid var(--line); }
  .spectrum { min-height: 0; }
  .pad { padding: .75rem; }
  .muted { color: var(--muted); }
  .acq.running { color: var(--ok); }
  .acq.failed { color: var(--bad); }
  .acq.external_busy { color: var(--warn); }
</style>
