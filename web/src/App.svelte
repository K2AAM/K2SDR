<script lang="ts">
  import { onMount } from "svelte";
  import { AudioPlayer } from "./lib/audio";
  import { Connection, type ConnStatus } from "./lib/client";
  import { formatFrequency } from "./lib/freq";
  import { Listener, loadSettings } from "./lib/listener";
  import type { Json } from "./lib/patch";
  import Recordings from "./Recordings.svelte";
  import Rig from "./Rig.svelte";
  import SpectrumView from "./SpectrumView.svelte";

  type Receiver = {
    label: string; driver: string; capture_gen: number;
    acquisition: { state: string; detail: string | null };
    capture: { center_hz: { applied: number | null }; sample_rate_hz: { applied: number | null } };
  };

  const conn = new Connection();
  const player = new AudioPlayer();
  let status: ConnStatus = $state("connecting");
  let receivers: Record<string, Receiver> = $state({});
  let available: string[] = $state([]);
  let selected: string | null = $state(null);
  let permissions: string[] = $state([]);
  let username = $state("");
  let csrf = $state("");
  let showRecordings = $state(false);
  type RecState = { receiver: string; owner: string; seconds: number; bytes: number; file: string };
  let recordings: Record<string, RecState> = $state({});
  let storage: { full: boolean; detail: string | null } = $state({ full: false, detail: null });
  let listener: Listener | null = $state(null);
  let listenerTick = $state(0);

  conn.onStatus((s) => (status = s));
  conn.onState(() => {
    const st = conn.state as { receivers?: Record<string, Json>; ledger?: { available?: string[] } } | null;
    receivers = (st?.receivers ?? {}) as unknown as Record<string, Receiver>;
    available = st?.ledger?.available ?? [];
    const full = conn.state as { recordings?: Json; storage?: Json } | null;
    recordings = (full?.recordings ?? {}) as unknown as Record<string, RecState>;
    storage = (full?.storage ?? { full: false, detail: null }) as unknown as typeof storage;
    if (selected === null || !(selected in receivers)) selected = Object.keys(receivers)[0] ?? null;
    ensureListener();
  });

  onMount(() => {
    conn.connect();
    fetch("/api/session", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j: { permissions?: string[]; username?: string; csrf?: string } | null) => {
        permissions = j?.permissions ?? [];
        username = j?.username ?? "";
        csrf = j?.csrf ?? "";
      })
      .catch(() => {});
    return () => listener?.close();
  });

  // One listener channel for the selected receiver; replaced when the selection changes.
  function ensureListener() {
    if (!selected || status !== "open" || !available.length) return;
    if (listener && listener.receiverId === selected) return;
    const rx = receivers[selected];
    const center = rx?.capture.center_hz.applied;
    if (!rx || rx.acquisition.state !== "running" || center == null) return;
    listener?.close();
    const s = loadSettings(selected, center);
    if (!available.includes(s.mode)) s.mode = available.includes("am") ? "am" : available[0]!;
    const l = new Listener(conn, selected, s, (pcm, flush) => { if (flush) player.flush(); player.push(pcm); });
    l.onChange(() => listenerTick++);
    listener = l;
    void l.open();
  }
  $effect(() => { void selected; void status; ensureListener(); });

  const rx = $derived(selected !== null ? receivers[selected] : undefined);
  const marker = $derived.by(() => {
    void listenerTick;
    const v = listener?.view;
    return v && v.passband_hz ? { hz: v.frequency_hz, loHz: v.passband_hz[0], hiHz: v.passband_hz[1] } : null;
  });
  const stepHz = $derived.by(() => { void listenerTick; return listener?.settings.stepHz ?? 1000; });

  const myRecording = $derived.by(() => {
    const e = Object.entries(recordings).find(([, r]) => r.receiver === selected && r.owner === `user:${username}`);
    return e ? { id: e[0], seconds: e[1].seconds, bytes: e[1].bytes, file: e[1].file } : null;
  });

  async function record() {
    if (!listener || !selected) return;
    const s = listener.settings;
    const args: Record<string, Json> = { receiver: selected, frequency_hz: listener.view?.frequency_hz ?? s.frequencyHz,
                                         mode: s.mode, agc: s.agc, manual_gain_db: s.manualGainDb };
    if (s.passbandHz) args.passband_hz = s.passbandHz;
    await conn.command("recording.start", args);
  }

  async function stopRecording(id: string) {
    await conn.command("recording.stop", { recording: id });
  }

  async function retune(hz: number) {
    if (!selected) return;
    await conn.command("receiver.claim", { receiver: selected });
    await conn.command("receiver.set_capture", { receiver: selected, center_hz: hz });
    await conn.command("receiver.release", { receiver: selected });
  }

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
  {#if permissions.includes("record")}
    <button class="hdrbtn" onclick={() => (showRecordings = !showRecordings)}>Recordings</button>
  {/if}
  <a class="status-link" href="/status">Status</a>
</header>

<main>
  {#if rx && selected}
    <section class="rx">
      <span class="acq {rx.acquisition.state}">
        {rx.acquisition.state}{rx.acquisition.detail ? ` — ${rx.acquisition.detail}` : ""}
      </span>
      <span>Centre {rx.capture.center_hz.applied !== null ? formatFrequency(rx.capture.center_hz.applied) : "—"}
        · {rx.capture.sample_rate_hz.applied ?? "—"} S/s · generation {rx.capture_gen}</span>
    </section>
    <div class="spectrum">
      {#key selected}
        <SpectrumView {conn} receiverId={selected} {marker} {stepHz}
                      onTune={(hz) => listener?.tune({ frequencyHz: hz })} />
      {/key}
    </div>
    {#if listener}
      {#key listener}
        <Rig {listener} {player} modes={available} canRetune={permissions.includes("hw.retune")} onRetune={retune}
             canRecord={permissions.includes("record")} recording={myRecording}
             storageFull={storage.full ? (storage.detail ?? "full") : null} onRecord={record}
             onStopRecording={stopRecording} />
      {/key}
    {/if}
  {:else}
    <p class="muted pad">No receivers configured.</p>
  {/if}
</main>
{#if showRecordings}
  <Recordings {csrf} onClose={() => (showRecordings = false)} />
{/if}

<style>
  :global(#app) { display: grid; grid-template-rows: auto 1fr; }
  header { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .75rem; padding: .4rem .75rem;
           background: var(--panel); border-bottom: 1px solid var(--line); }
  .brand { font-weight: 600; }
  .conn { margin-left: auto; color: var(--muted); }
  .conn.open { color: var(--ok); }
  .conn.reconnecting { color: var(--warn); }
  .status-link { color: var(--accent); }
  .hdrbtn { background: #0f1215; border: 1px solid var(--line); border-radius: 4px; padding: .15rem .5rem; cursor: pointer; }
  main { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-height: 0; }
  .rx { display: flex; flex-wrap: wrap; gap: .3rem 1rem; padding: .3rem .75rem; font-size: 13px;
        border-bottom: 1px solid var(--line); }
  .spectrum { min-height: 0; }
  .pad { padding: .75rem; }
  .muted { color: var(--muted); }
  .acq.running { color: var(--ok); }
  .acq.failed { color: var(--bad); }
  .acq.external_busy { color: var(--warn); }
</style>
