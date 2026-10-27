<script lang="ts">
  // Spectrum trace + waterfall for one receiver (stream v1 SPECTRUM). Rows of an older capture
  // generation are dropped here too (the server already never sends them); a new generation draws
  // a separator line in the waterfall. Click or tap to tune (snapped to the step).
  import { onMount } from "svelte";
  import type { Connection } from "./lib/client";
  import { Flags, type SpectrumMsg } from "./lib/stream";
  import {
    autoLevels, binsToDb, colormap, hzToPixel, levelIndex, pixelToHz, resampleMax, snap, ticks, type Levels,
  } from "./lib/spectrum";

  interface Props {
    conn: Connection;
    receiverId: string;
    stepHz?: number;
    marker?: { hz: number; loHz: number; hiHz: number } | null;
    onTune?: (hz: number) => void;
  }
  let { conn, receiverId, stepHz = 1000, marker = null, onTune }: Props = $props();

  let specCanvas: HTMLCanvasElement | undefined = $state();
  let fallCanvas: HTMLCanvasElement | undefined = $state();
  let auto = $state(true);
  let manualMin = $state(-120);
  let manualMax = $state(-40);
  let span: { centerHz: number; rateHz: number } | null = $state(null);
  let levels: Levels | null = null;
  let lastGen = -1;
  let lastRow: Float32Array | null = null;
  const lut = colormap();

  // Subscribe while mounted; again after every reconnect (subscriptions are per socket session).
  let subscribed: string | null = null;
  function subscribe(id: string) {
    if (subscribed && subscribed !== id) void conn.command("stream.spectrum", { receiver: subscribed, enabled: false });
    subscribed = id;
    lastGen = -1;
    void conn.command("stream.spectrum", { receiver: id, enabled: true });
  }
  $effect(() => { subscribe(receiverId); });
  onMount(() => {
    const offConnect = conn.onConnect(() => {
      if (subscribed) { lastGen = -1; void conn.command("stream.spectrum", { receiver: subscribed, enabled: true }); }
    });
    const offStream = conn.onStream((m) => { if (m.kind === 1 && m.id === receiverId) onRow(m); });
    return () => {
      offConnect();
      offStream();
      if (subscribed) void conn.command("stream.spectrum", { receiver: subscribed, enabled: false });
    };
  });

  function fit(c: HTMLCanvasElement): boolean {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; return true; }
    return false;
  }

  function onRow(m: SpectrumMsg) {
    if (m.gen < lastGen) return;                               // stale generation: never shown
    const newGen = m.gen > lastGen;
    lastGen = m.gen;
    span = { centerHz: m.frequencyHz, rateHz: m.sampleRateHz };
    const db = binsToDb(m.bins, m.dbFloorMdb, m.dbStepMdb);
    lastRow = db;
    levels = auto ? autoLevels(db, newGen ? null : levels) : { minDb: manualMin, maxDb: manualMax };
    drawWaterfall(db, newGen || (m.flags & Flags.NEW_GEN) !== 0, (m.flags & (Flags.DISCONTINUITY | Flags.SERVER_DROP)) !== 0);
    drawSpectrum();
  }

  function drawWaterfall(db: Float32Array, separator: boolean, gap: boolean) {
    const c = fallCanvas;
    if (!c || !levels) return;
    fit(c);
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const w = c.width, h = c.height;
    const rh = Math.max(1, Math.round(window.devicePixelRatio || 1));   // one CSS pixel per row on any screen
    ctx.drawImage(c, 0, 0, w, h - rh, 0, rh, w, h - rh);        // scroll down one row
    const px = resampleMax(db, w);
    const row = ctx.createImageData(w, 1);
    for (let x = 0; x < w; x++) {
      const k = levelIndex(px[x]!, levels) * 4;
      row.data[x * 4] = lut[k]!; row.data[x * 4 + 1] = lut[k + 1]!; row.data[x * 4 + 2] = lut[k + 2]!; row.data[x * 4 + 3] = 255;
    }
    if (separator) {                                            // new capture generation: a full white line
      for (let x = 0; x < w; x++) row.data.set([255, 255, 255, 255], x * 4);
    } else if (gap) {                                           // samples missing: a short orange tick
      for (let x = 0; x < Math.min(w, 8 * (window.devicePixelRatio || 1)); x++) row.data.set([255, 140, 0, 255], x * 4);
    }
    for (let r = 0; r < rh; r++) ctx.putImageData(row, 0, r);
  }

  function drawSpectrum() {
    const c = specCanvas;
    if (!c || !levels || !lastRow || !span) return;
    fit(c);
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.width, h = c.height, axis = 16 * dpr;
    ctx.clearRect(0, 0, w, h);
    const plotH = h - axis;
    const y = (db: number) => plotH - ((db - levels!.minDb) / (levels!.maxDb - levels!.minDb)) * plotH;
    // dB grid every 10 dB
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.fillStyle = "rgba(255,255,255,0.45)";
    ctx.font = `${10 * dpr}px system-ui, sans-serif`;
    for (let d = Math.ceil(levels.minDb / 10) * 10; d <= levels.maxDb; d += 10) {
      ctx.beginPath(); ctx.moveTo(0, y(d)); ctx.lineTo(w, y(d)); ctx.stroke();
      ctx.fillText(`${d}`, 3 * dpr, y(d) - 2 * dpr);
    }
    // passband of the listener's channel
    if (marker) {
      const x0 = hzToPixel(marker.hz + marker.loHz, w, span.centerHz, span.rateHz);
      const x1 = hzToPixel(marker.hz + marker.hiHz, w, span.centerHz, span.rateHz);
      ctx.fillStyle = "rgba(79,163,255,0.18)";
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), plotH);
      const xc = hzToPixel(marker.hz, w, span.centerHz, span.rateHz);
      ctx.fillStyle = "#4fa3ff";
      ctx.fillRect(xc, 0, Math.max(1, dpr), plotH);
    }
    // trace
    const px = resampleMax(lastRow, w);
    ctx.strokeStyle = "#e3e6e9";
    ctx.lineWidth = dpr;
    ctx.beginPath();
    for (let x = 0; x < w; x++) (x ? ctx.lineTo : ctx.moveTo).call(ctx, x, Math.max(0, Math.min(plotH, y(px[x]!))));
    ctx.stroke();
    // frequency axis (MHz labels; values are integer Hz)
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.textAlign = "center";
    for (const t of ticks(span.centerHz, span.rateHz, Math.max(3, Math.round(c.clientWidth / 110)))) {
      const x = hzToPixel(t.hz, w, span.centerHz, span.rateHz);
      ctx.fillRect(x, plotH, 1, 4 * dpr);
      ctx.fillText(t.label, x, h - 3 * dpr);
    }
    ctx.textAlign = "start";
  }

  function tuneAt(ev: MouseEvent) {
    if (!onTune || !span) return;
    const el = ev.currentTarget as HTMLCanvasElement;
    const r = el.getBoundingClientRect();
    onTune(snap(pixelToHz(ev.clientX - r.left, r.width, span.centerHz, span.rateHz), stepHz));
  }

  $effect(() => { void marker; drawSpectrum(); });
  $effect(() => { if (!auto) { levels = { minDb: manualMin, maxDb: Math.max(manualMin + 1, manualMax) }; drawSpectrum(); } });
</script>

<div class="view">
  <canvas class="spec" bind:this={specCanvas} onclick={tuneAt} aria-label="Spectrum"></canvas>
  <canvas class="fall" bind:this={fallCanvas} onclick={tuneAt} aria-label="Waterfall"></canvas>
  <div class="levels">
    <label><input type="checkbox" bind:checked={auto} /> Auto levels</label>
    {#if !auto}
      <label>min <input type="number" step="5" bind:value={manualMin} aria-label="Minimum dBFS" /> dB</label>
      <label>max <input type="number" step="5" bind:value={manualMax} aria-label="Maximum dBFS" /> dB</label>
    {/if}
  </div>
</div>

<style>
  .view { position: relative; display: grid; grid-template-rows: minmax(0, 1fr) minmax(0, 2fr); height: 100%; min-height: 0; }
  canvas { width: 100%; height: 100%; min-height: 0; display: block; cursor: crosshair; touch-action: manipulation; }
  .spec { background: #0b0e11; border-bottom: 1px solid var(--line); }
  .fall { background: #000; }
  .levels { position: absolute; top: 4px; right: 6px; display: flex; gap: .6rem; font-size: 12px;
            background: rgba(15,18,21,.75); padding: 2px 6px; border-radius: 4px; }
  .levels input[type="number"] { width: 4.2em; background: transparent; border: 1px solid var(--line); border-radius: 3px; }
</style>
