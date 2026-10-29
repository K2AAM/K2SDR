<script lang="ts">
  // Spectrum trace + waterfall for one receiver (stream v1 SPECTRUM). Rows of an older capture
  // generation are dropped here too (the server already never sends them); a new generation draws
  // a separator line in the waterfall. Click or tap to tune (snapped to the step); the mouse wheel
  // tunes by steps; Ctrl/⌘ + wheel or a trackpad pinch zooms around the pointer, as do − / +.
  import { onMount } from "svelte";
  import type { Connection } from "./lib/client";
  import { Flags, type SpectrumMsg } from "./lib/stream";
  import {
    autoLevels, binsToDb, captureView, clampView, colormap, hzToPixel, levelIndex, MAX_ZOOM, pixelToHz,
    rowToPixels, snap, ticks, wheelSteps, zoomOf, zoomView, type Levels, type View,
  } from "./lib/spectrum";

  interface Props {
    conn: Connection;
    receiverId: string;
    stepHz?: number;
    marker?: { hz: number; loHz: number; hiHz: number } | null;
    onTune?: (hz: number) => void;
    onNudge?: (steps: number) => void;
  }
  let { conn, receiverId, stepHz = 1000, marker = null, onTune, onNudge }: Props = $props();

  let specCanvas: HTMLCanvasElement | undefined = $state();
  let fallCanvas: HTMLCanvasElement | undefined = $state();
  let auto = $state(true);
  let manualMin = $state(-120);
  let manualMax = $state(-40);
  let span: { centerHz: number; rateHz: number } | null = $state(null);
  let view: View | null = $state(null);                        // shown window; null: the whole capture
  let levels: Levels | null = null;
  let lastGen = -1;
  let lastRow: Float32Array | null = null;
  const lut = colormap();

  // Waterfall history: the newest rows with the span each was captured at, so the whole waterfall
  // can be redrawn exactly after a zoom or a resize (iOS resizes on every toolbar show/hide).
  interface Row { db: Float32Array; centerHz: number; rateHz: number; mark: "sep" | "gap" | null }
  const MAX_ROWS = 1500;
  let rows: Row[] = [];

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
    if (c.width === w && c.height === h) return false;
    c.width = w; c.height = h;                                 // clears the canvas
    return true;
  }

  /** The window on screen: the zoomed view, kept inside the current capture. */
  function shown(): View | null {
    if (!span) return null;
    if (!view) return captureView(span.centerHz, span.rateHz);
    return clampView((view.loHz + view.hiHz) / 2, view.hiHz - view.loHz, span.centerHz, span.rateHz);
  }

  function onRow(m: SpectrumMsg) {
    if (m.gen < lastGen) return;                               // stale generation: never shown
    const newGen = m.gen > lastGen;
    lastGen = m.gen;
    const moved = !span || span.centerHz !== m.frequencyHz || span.rateHz !== m.sampleRateHz;
    span = { centerHz: m.frequencyHz, rateHz: m.sampleRateHz };
    if (moved && view) {                                        // receiver retuned: keep the zoom, follow the listener
      const w = view.hiHz - view.loHz;
      const focus = marker?.hz ?? (view.loHz + view.hiHz) / 2;
      view = clampView(focus, w, span.centerHz, span.rateHz);
    }
    const db = binsToDb(m.bins, m.dbFloorMdb, m.dbStepMdb);
    lastRow = db;
    const v = shown()!;
    const visible = rowToPixels(db, span.centerHz, span.rateHz, v, 512).filter((x) => !Number.isNaN(x));
    levels = auto ? autoLevels(visible, newGen ? null : levels) : { minDb: manualMin, maxDb: manualMax };
    const mark = newGen || (m.flags & Flags.NEW_GEN) !== 0 ? "sep"
      : (m.flags & (Flags.DISCONTINUITY | Flags.SERVER_DROP)) !== 0 ? "gap" : null;
    rows.unshift({ db, centerHz: span.centerHz, rateHz: span.rateHz, mark });
    if (rows.length > MAX_ROWS) rows.length = MAX_ROWS;
    drawNewRow(rows[0]!);
    drawSpectrum();
  }

  function paintRow(ctx: CanvasRenderingContext2D, r: Row, v: View, w: number, y: number, rh: number) {
    const px = rowToPixels(r.db, r.centerHz, r.rateHz, v, w);
    const img = ctx.createImageData(w, 1);
    for (let x = 0; x < w; x++) {
      const val = px[x]!;
      if (Number.isNaN(val)) { img.data[x * 4 + 3] = 255; continue; }   // outside that row's capture: black
      const k = levelIndex(val, levels!) * 4;
      img.data[x * 4] = lut[k]!; img.data[x * 4 + 1] = lut[k + 1]!; img.data[x * 4 + 2] = lut[k + 2]!; img.data[x * 4 + 3] = 255;
    }
    const dpr = window.devicePixelRatio || 1;
    if (r.mark === "sep") {                                     // new capture generation: a full white line
      for (let x = 0; x < w; x++) img.data.set([255, 255, 255, 255], x * 4);
    } else if (r.mark === "gap") {                              // samples missing: a short orange tick
      for (let x = 0; x < Math.min(w, 8 * dpr); x++) img.data.set([255, 140, 0, 255], x * 4);
    }
    for (let i = 0; i < rh; i++) ctx.putImageData(img, 0, y + i);
  }

  const rowH = () => Math.max(1, Math.round(window.devicePixelRatio || 1));   // one CSS pixel per row

  function drawNewRow(r: Row) {
    const c = fallCanvas, v = shown();
    if (!c || !levels || !v) return;
    if (fit(c)) { redrawWaterfall(); return; }
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const w = c.width, h = c.height, rh = rowH();
    ctx.drawImage(c, 0, 0, w, h - rh, 0, rh, w, h - rh);        // scroll down one row
    paintRow(ctx, r, v, w, 0, rh);
  }

  function redrawWaterfall() {
    const c = fallCanvas, v = shown();
    if (!c || !levels || !v) return;
    fit(c);
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const w = c.width, h = c.height, rh = rowH();
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    for (let i = 0; i < rows.length && i * rh < h; i++) paintRow(ctx, rows[i]!, v, w, i * rh, rh);
  }

  function drawSpectrum() {
    const c = specCanvas, v = shown();
    if (!c || !levels || !lastRow || !span || !v) return;
    fit(c);
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.width, h = c.height, axis = 16 * dpr;
    const vc = (v.loHz + v.hiHz) / 2, vw = v.hiHz - v.loHz;
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
      const x0 = hzToPixel(marker.hz + marker.loHz, w, vc, vw);
      const x1 = hzToPixel(marker.hz + marker.hiHz, w, vc, vw);
      ctx.fillStyle = "rgba(79,163,255,0.18)";
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), plotH);
      const xc = hzToPixel(marker.hz, w, vc, vw);
      ctx.fillStyle = "#4fa3ff";
      ctx.fillRect(xc, 0, Math.max(1, dpr), plotH);
    }
    // trace
    const px = rowToPixels(lastRow, span.centerHz, span.rateHz, v, w);
    ctx.strokeStyle = "#e3e6e9";
    ctx.lineWidth = dpr;
    ctx.beginPath();
    for (let x = 0; x < w; x++) (x ? ctx.lineTo : ctx.moveTo).call(ctx, x, Math.max(0, Math.min(plotH, y(px[x]!))));
    ctx.stroke();
    // frequency axis (MHz labels; values are integer Hz)
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.textAlign = "center";
    for (const t of ticks(vc, vw, Math.max(3, Math.round(c.clientWidth / 110)))) {
      const x = hzToPixel(t.hz, w, vc, vw);
      ctx.fillRect(x, plotH, 1, 4 * dpr);
      ctx.fillText(t.label, x, h - 3 * dpr);
    }
    ctx.textAlign = "start";
  }

  function hzAt(ev: MouseEvent): number | null {
    const v = shown();
    if (!v) return null;
    const r = (ev.currentTarget as HTMLCanvasElement).getBoundingClientRect();
    return pixelToHz(ev.clientX - r.left, r.width, (v.loHz + v.hiHz) / 2, v.hiHz - v.loHz);
  }

  function tuneAt(ev: MouseEvent) {
    const hz = hzAt(ev);
    if (onTune && hz !== null) onTune(snap(hz, stepHz));
  }

  function setZoom(factor: number, anchorHz?: number) {
    const v = shown();
    if (!v || !span) return;
    const z = zoomView(v, factor, anchorHz ?? marker?.hz ?? (v.loHz + v.hiHz) / 2, span.centerHz, span.rateHz);
    view = zoomOf(z, span.rateHz) <= 1.0001 ? null : z;
    redrawWaterfall();
    drawSpectrum();
  }

  let wheelAcc = 0;
  function wheel(ev: WheelEvent) {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {                             // Ctrl/⌘ + wheel, or a trackpad pinch
      const hz = hzAt(ev);
      setZoom(Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.05 : 0.002)), hz ?? undefined);
      return;
    }
    const r = wheelSteps(wheelAcc, ev);
    wheelAcc = r.acc;
    if (r.steps && onNudge) onNudge(r.steps);
  }

  const zoomLabel = $derived.by(() => {
    const sp = span as { centerHz: number; rateHz: number } | null, v = view as View | null;
    return sp && v ? Math.round(zoomOf(v, sp.rateHz) * 10) / 10 : 1;
  });

  $effect(() => { void marker; drawSpectrum(); });
  $effect(() => {
    if (!auto) { levels = { minDb: manualMin, maxDb: Math.max(manualMin + 1, manualMax) }; drawSpectrum(); redrawWaterfall(); }
  });
</script>

<div class="view">
  <!-- Each canvas fills a box of its own, out of the layout flow: a canvas's pixel size (set from
       its CSS size x DPR) must never feed back into the CSS size (it grew the page 3x per redraw). -->
  <div class="box spec"><canvas bind:this={specCanvas} onclick={tuneAt} onwheel={wheel} aria-label="Spectrum"></canvas></div>
  <div class="box fall"><canvas class="fall" bind:this={fallCanvas} onclick={tuneAt} onwheel={wheel} aria-label="Waterfall"></canvas></div>
  <div class="levels">
    <span class="zoom">
      <button onclick={() => setZoom(0.5)} disabled={!view} aria-label="Zoom out">−</button>
      <span aria-label="Zoom">×{zoomLabel}</span>
      <button onclick={() => setZoom(2)} disabled={zoomLabel >= MAX_ZOOM} aria-label="Zoom in">+</button>
    </span>
    <label><input type="checkbox" bind:checked={auto} /> Auto levels</label>
    {#if !auto}
      <label>min <input type="number" step="5" bind:value={manualMin} aria-label="Minimum dBFS" /> dB</label>
      <label>max <input type="number" step="5" bind:value={manualMax} aria-label="Maximum dBFS" /> dB</label>
    {/if}
  </div>
</div>

<style>
  .view { position: relative; display: grid; grid-template-rows: minmax(0, 1fr) minmax(0, 2fr);
          grid-template-columns: minmax(0, 1fr); height: 100%; min-height: 0; min-width: 0; overflow: hidden; }
  .box { position: relative; min-height: 0; min-width: 0; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; cursor: crosshair;
           touch-action: manipulation; }
  .spec { background: #0b0e11; border-bottom: 1px solid var(--line); }
  .fall { background: #000; }
  .levels { position: absolute; top: 4px; right: 6px; display: flex; gap: .6rem; font-size: 12px;
            background: rgba(15,18,21,.75); padding: 2px 6px; border-radius: 4px; }
  .zoom { display: inline-flex; align-items: center; gap: .3rem; font-variant-numeric: tabular-nums; }
  .zoom button { min-width: 1.6rem; background: #0f1215; border: 1px solid var(--line); border-radius: 3px; cursor: pointer; }
  .zoom button:disabled { opacity: .4; cursor: default; }
  .levels input[type="number"] { width: 4.2em; background: transparent; border: 1px solid var(--line); border-radius: 3px; }
</style>
