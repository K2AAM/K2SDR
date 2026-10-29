<script lang="ts">
  // Compact RIG (outline §3; design §3.5): frequency with unit-explicit entry (the interpreted value
  // is shown before tuning), step, mode, passband, AGC, S-meter (dBFS, uncalibrated), volume/mute.
  import { onMount } from "svelte";
  import type { AudioPlayer } from "./lib/audio";
  import type { Outcome } from "./lib/client";
  import { FrequencyParseError, formatFrequency, parseFrequency } from "./lib/freq";
  import type { Agc, Listener } from "./lib/listener";

  interface Props {
    listener: Listener;
    player: AudioPlayer;
    modes: string[];
    canRetune: boolean;
    onRetune: (hz: number, endRecordings?: boolean) => Promise<Outcome | null>;
    canRecord: boolean;
    recording: { id: string; seconds: number; bytes: number; file: string } | null;   // mine, on this receiver
    storageFull: string | null;
    onRecord: () => Promise<void>;
    onStopRecording: (id: string) => Promise<void>;
  }
  let { listener, player, modes, canRetune, onRetune, canRecord, recording, storageFull, onRecord,
        onStopRecording }: Props = $props();
  let recBusy = $state(false);
  async function toggleRecord() {
    recBusy = true;
    try {
      if (recording) await onStopRecording(recording.id);
      else await onRecord();
    } finally {
      recBusy = false;
    }
  }

  const STEPS = [10, 100, 500, 1000, 5000, 9000, 10000];
  let tick = $state(0);                                     // re-render on listener/player changes
  onMount(() => {
    const a = listener.onChange(() => tick++);
    const b = player.onChange(() => tick++);
    return () => { a(); b(); };
  });

  const view = $derived.by(() => { void tick; return listener.view; });
  const s = $derived.by(() => { void tick; return listener.settings; });
  const freqHz = $derived(view?.frequency_hz ?? s.frequencyHz);
  const level = $derived.by(() => { void tick; return listener.levelDbfs; });
  const err = $derived.by(() => { void tick; return listener.lastError; });
  const stats = $derived.by(() => { void tick; return player.stats; });
  const needsTap = $derived.by(() => { void tick; return player.needsTap; });

  let entry = $state("");
  let unit = $state("kHz");
  type Preview = { hz: number; error?: undefined } | { hz?: undefined; error: string } | null;
  const preview: Preview = $derived.by((): Preview => {
    if (!entry.trim()) return null;
    try {
      return { hz: parseFrequency(entry, { defaultUnit: unit }) };
    } catch (e) {
      return { error: e instanceof FrequencyParseError ? e.message : String(e) };
    }
  });
  let pendingRetune: number | null = $state(null);

  async function tuneTo(hz: number) {
    pendingRetune = null;
    const ok = await listener.tune({ frequencyHz: hz });
    if (!ok && listener.lastError?.code === "E_OUT_OF_RANGE" && canRetune) pendingRetune = hz;
  }
  async function submit(ev: SubmitEvent) {
    ev.preventDefault();
    if (preview?.hz !== undefined) { await tuneTo(preview.hz); entry = ""; }
  }
  // A retune that would end an active recording (anyone's) is refused until confirmed.
  let recordingWarning: { hz: number; detail: string } | null = $state(null);
  async function retune(endRecordings = false) {
    const hz = recordingWarning?.hz ?? pendingRetune;
    if (hz === null) return;
    pendingRetune = null;
    recordingWarning = null;
    const o = await onRetune(hz, endRecordings);
    const src = o?.result ?? o?.ack;
    if (src?.code === "E_RECORDING_ACTIVE") {
      recordingWarning = { hz, detail: src.detail ?? "" };
      return;
    }
    await listener.tune({ frequencyHz: hz });
  }
  function wheel(ev: WheelEvent) {
    ev.preventDefault();
    void tuneTo(freqHz + (ev.deltaY < 0 ? 1 : -1) * s.stepHz);
  }

  let loEdit = $state<number | null>(null);
  let hiEdit = $state<number | null>(null);
  const pb = $derived(view?.passband_hz ?? null);
  async function applyPassband() {
    const lo = loEdit ?? pb?.[0], hi = hiEdit ?? pb?.[1];
    if (lo === undefined || hi === undefined) return;
    if (await listener.tune({ passbandHz: [lo, hi] })) { loEdit = null; hiEdit = null; }
  }

  let volume = $state(0.8);
  let muted = $state(false);
  $effect(() => { player.setVolume(volume); });
  $effect(() => { player.setMuted(muted); });

  const meterPct = $derived(level === null ? 0 : Math.max(0, Math.min(100, ((level + 130) / 130) * 100)));
</script>

<div class="rig">
  <div class="freq" onwheel={wheel}>
    <button class="stepbtn" onclick={() => tuneTo(freqHz - s.stepHz)} aria-label="Step down">−</button>
    <output class="big" aria-label="Frequency">{formatFrequency(freqHz)}</output>
    <button class="stepbtn" onclick={() => tuneTo(freqHz + s.stepHz)} aria-label="Step up">+</button>
  </div>

  <form class="entry" onsubmit={submit}>
    <input bind:value={entry} placeholder="e.g. 7074 kHz" aria-label="Frequency entry" inputmode="decimal"
           autocomplete="off" spellcheck="false" />
    <select bind:value={unit} aria-label="Unit for a bare number">
      <option>Hz</option><option>kHz</option><option>MHz</option>
    </select>
    <button type="submit" disabled={preview?.hz === undefined}>Tune</button>
    {#if preview?.hz !== undefined}
      <span class="hint">→ {formatFrequency(preview.hz)}</span>
    {:else if preview}
      <span class="hint bad">{preview.error}</span>
    {/if}
  </form>

  <div class="row">
    <label>Mode
      <select value={s.mode} onchange={(e) => listener.tune({ mode: (e.currentTarget as HTMLSelectElement).value })}>
        {#each modes as m (m)}<option value={m}>{m.toUpperCase()}</option>{/each}
      </select>
    </label>
    <label>Step
      <select value={s.stepHz} onchange={(e) => listener.tune({ stepHz: Number((e.currentTarget as HTMLSelectElement).value) })}>
        {#each STEPS as st (st)}<option value={st}>{st >= 1000 ? `${st / 1000} kHz` : `${st} Hz`}</option>{/each}
      </select>
    </label>
    <label>AGC
      <select value={s.agc} onchange={(e) => listener.tune({ agc: (e.currentTarget as HTMLSelectElement).value as Agc })}>
        <option value="fast">Fast</option><option value="medium">Medium</option><option value="slow">Slow</option>
        <option value="off">Off</option>
      </select>
    </label>
    {#if s.agc === "off"}
      <label>Gain
        <input type="range" min="0" max="100" value={s.manualGainDb} aria-label="Manual gain dB"
               onchange={(e) => listener.tune({ manualGainDb: Number((e.currentTarget as HTMLInputElement).value) })} />
        {s.manualGainDb} dB
      </label>
    {/if}
  </div>

  <div class="row">
    <span>Passband</span>
    <input class="hz" type="number" step="50" value={loEdit ?? pb?.[0] ?? ""} aria-label="Passband low Hz"
           oninput={(e) => (loEdit = Number((e.currentTarget as HTMLInputElement).value))} />
    <span>…</span>
    <input class="hz" type="number" step="50" value={hiEdit ?? pb?.[1] ?? ""} aria-label="Passband high Hz"
           oninput={(e) => (hiEdit = Number((e.currentTarget as HTMLInputElement).value))} />
    <span>Hz</span>
    <button onclick={applyPassband} disabled={loEdit === null && hiEdit === null}>Set</button>
    <button onclick={() => listener.tune({ passbandDefault: true })} disabled={view?.passband_default ?? true}>Default</button>
  </div>

  <div class="row">
    <div class="meter" aria-label="Signal level">
      <div class="bar" style:width="{meterPct}%"></div>
    </div>
    <span class="lvl">{level === null ? "—" : `${level.toFixed(1)} dBFS`} <span class="muted">(uncalibrated)</span></span>
  </div>

  <div class="row">
    {#if needsTap}
      <button class="tap" onclick={() => player.startFromGesture()}>▶ Tap to start audio</button>
    {:else}
      <button onclick={() => (muted = !muted)} aria-pressed={muted}>{muted ? "Unmute" : "Mute"}</button>
      <input type="range" min="0" max="1" step="0.01" bind:value={volume} aria-label="Volume" />
      {#if stats}<span class="muted small jit">buffer {stats.bufferedMs} ms · underruns {stats.underruns}</span>{/if}
    {/if}
  </div>

  {#if canRecord}
    <div class="row">
      <button class="rec" class:on={recording !== null} onclick={toggleRecord}
              disabled={recBusy || (recording === null && storageFull !== null)}>
        {recording ? "■ Stop recording" : "● Record"}
      </button>
      {#if recording}
        <span class="small recinfo">{Math.floor(recording.seconds / 60)}:{String(Math.floor(recording.seconds) % 60).padStart(2, "0")}
          · {(recording.bytes / 1048576).toFixed(1)} MB · {recording.file}</span>
      {:else if storageFull}
        <span class="bad small">Recording storage full: {storageFull}</span>
      {:else}
        <span class="muted small">records this frequency, mode and filter on the server (continues if the browser closes)</span>
      {/if}
    </div>
  {/if}

  {#if recordingWarning}
    <div class="warn" role="alert">
      Still recording: retuning to {formatFrequency(recordingWarning.hz)} would end it.
      <span class="muted small">({recordingWarning.detail.split(": ")[1]?.split("; send")[0] ?? ""})</span>
      <button onclick={() => retune(true)}>End recording and retune</button>
      <button onclick={() => (recordingWarning = null)}>Cancel</button>
    </div>
  {/if}

  {#if err}
    <div class="err" role="alert">{err.detail || err.code}
      {#if pendingRetune !== null}
        <button onclick={() => retune()}>Retune receiver to {formatFrequency(pendingRetune)}</button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .rig { display: grid; gap: .45rem; padding: .5rem .75rem; background: var(--panel); border-top: 1px solid var(--line); }
  .freq { display: flex; align-items: center; gap: .5rem; }
  .big { font-size: 1.6rem; font-variant-numeric: tabular-nums; letter-spacing: .02em; min-width: 11ch; text-align: center; }
  .stepbtn { width: 2.2rem; height: 2.2rem; font-size: 1.2rem; }
  .entry, .row { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem .7rem; }
  .entry input { width: 10em; }
  .hint { color: var(--muted); font-variant-numeric: tabular-nums; }
  .bad, .err { color: var(--bad); }
  .warn { color: var(--warn); display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; }
  .hz { width: 6em; }
  button, select, input { background: #0f1215; border: 1px solid var(--line); border-radius: 4px; padding: .2rem .45rem; }
  button { cursor: pointer; }
  button:disabled { opacity: .45; cursor: default; }
  .meter { width: 12rem; height: .6rem; background: #0b0e11; border: 1px solid var(--line); border-radius: 3px; overflow: hidden; }
  .bar { height: 100%; background: linear-gradient(90deg, var(--ok), var(--warn), var(--bad)); }
  .lvl { font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
  .rec { font-weight: 600; }
  .rec.on { background: var(--bad); color: #fff; border-color: var(--bad); }
  .tap { background: var(--accent); color: #fff; border: none; padding: .45rem .9rem; font-weight: 600; }
</style>
