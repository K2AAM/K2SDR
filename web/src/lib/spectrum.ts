// Pure helpers for the spectrum and waterfall (no DOM): levels, resampling, colours, axis.
// Bins arrive as uint8 q with dB = floor + q * step (stream v1, milli-dB fields).

export interface Levels { minDb: number; maxDb: number }

export function binsToDb(bins: Uint8Array, floorMdb: number, stepMdb: number): Float32Array {
  const out = new Float32Array(bins.length);
  const f = floorMdb / 1000, s = stepMdb / 1000;
  for (let i = 0; i < bins.length; i++) out[i] = f + s * bins[i]!;
  return out;
}

/** Percentile of a dB row (p in 0..1). */
export function percentile(db: Float32Array, p: number): number {
  if (db.length === 0) return 0;
  const sorted = Float32Array.from(db).sort();
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(p * (sorted.length - 1))));
  return sorted[i]!;
}

/** Automatic display range: the noise floor (20th percentile) sits near the bottom, the strongest
 *  signal near the top, at least `minSpan` dB apart. Smoothed by `alpha` against `prev`. */
export function autoLevels(db: Float32Array, prev: Levels | null, alpha = 0.15, minSpan = 30): Levels {
  const noise = percentile(db, 0.2);
  let peak = db.length ? db.reduce((a, b) => (b > a ? b : a), -Infinity) : 0;
  peak = Math.max(peak, noise + minSpan - 10);
  const target = { minDb: noise - 10, maxDb: peak + 5 };
  if (!prev) return target;
  return { minDb: prev.minDb + alpha * (target.minDb - prev.minDb), maxDb: prev.maxDb + alpha * (target.maxDb - prev.maxDb) };
}

/** Resample a row to `width` pixels: each pixel shows the strongest bin it covers (signals never
 *  disappear when zoomed out), or the nearest bin when a pixel is narrower than a bin. */
export function resampleMax(db: Float32Array, width: number): Float32Array {
  const n = db.length, out = new Float32Array(width);
  for (let x = 0; x < width; x++) {
    const a = Math.floor((x * n) / width), b = Math.max(a + 1, Math.floor(((x + 1) * n) / width));
    let m = -Infinity;
    for (let i = a; i < b && i < n; i++) if (db[i]! > m) m = db[i]!;
    out[x] = m;
  }
  return out;
}

/** 256-entry RGBA colour map (dark blue -> cyan -> yellow -> red -> white), index = level 0..255. */
export function colormap(): Uint8ClampedArray {
  const stops: [number, number, number, number][] = [
    [0, 0, 0, 16], [0.25, 0, 40, 140], [0.45, 0, 170, 200], [0.65, 240, 220, 40], [0.85, 240, 40, 20], [1, 255, 255, 255],
  ];
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let j = 0;
    while (j < stops.length - 2 && t > stops[j + 1]![0]) j++;
    const [t0, r0, g0, b0] = stops[j]!, [t1, r1, g1, b1] = stops[j + 1]!;
    const u = (t - t0) / (t1 - t0);
    lut.set([r0 + u * (r1 - r0), g0 + u * (g1 - g0), b0 + u * (b1 - b0), 255], i * 4);
  }
  return lut;
}

/** dB -> colour index 0..255 for the current levels. */
export function levelIndex(db: number, lv: Levels): number {
  const t = (db - lv.minDb) / Math.max(1e-6, lv.maxDb - lv.minDb);
  return Math.max(0, Math.min(255, Math.round(t * 255)));
}

/** Frequency (integer Hz) at pixel x of `width` across a capture span: centre +- rate/2. */
export function pixelToHz(x: number, width: number, centerHz: number, rateHz: number): number {
  return Math.round(centerHz - rateHz / 2 + (x / width) * rateHz);
}

export function hzToPixel(hz: number, width: number, centerHz: number, rateHz: number): number {
  return ((hz - (centerHz - rateHz / 2)) / rateHz) * width;
}

/** Round to a tuning step (integer Hz). */
export function snap(hz: number, stepHz: number): number {
  return Math.round(hz / stepHz) * stepHz;
}

/** Axis ticks: about `target` labelled ticks at a 1/2/5 x 10^k Hz spacing inside the span. */
export function ticks(centerHz: number, rateHz: number, target = 8): { hz: number; label: string }[] {
  const raw = rateHz / target;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw)!;
  const lo = centerHz - rateHz / 2, hi = centerHz + rateHz / 2;
  const out: { hz: number; label: string }[] = [];
  for (let hz = Math.ceil(lo / step) * step; hz <= hi; hz += step) {
    out.push({ hz, label: (hz / 1e6).toFixed(step >= 1e6 ? 0 : step >= 1e5 ? 1 : step >= 1e4 ? 2 : 3) });
  }
  return out;
}
