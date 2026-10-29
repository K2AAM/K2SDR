import { describe, expect, it } from "vitest";
import {
  autoLevels, binsToDb, captureView, clampView, colormap, hzToPixel, levelIndex, MAX_ZOOM, percentile, pixelToHz,
  resampleMax, rowToPixels, snap, ticks, wheelSteps, zoomOf, zoomView,
}
  from "./spectrum";

describe("spectrum helpers", () => {
  it("decodes bins with the stream's milli-dB floor and step", () => {
    expect(Array.from(binsToDb(Uint8Array.from([0, 1, 255]), -127500, 500))).toEqual([-127.5, -127, 0]);
  });
  it("auto levels put the noise near the bottom and the peak near the top, smoothed", () => {
    const db = new Float32Array(1000).fill(-100);
    db[500] = -40;
    const lv = autoLevels(db, null);
    expect(lv.minDb).toBe(-110);
    expect(lv.maxDb).toBe(-35);
    const next = autoLevels(new Float32Array(1000).fill(-80), lv, 0.5);
    expect(next.minDb).toBeCloseTo(-100);                  // halfway to -90
    expect(percentile(db, 0.2)).toBe(-100);
  });
  it("auto levels keep a minimum span on a flat band", () => {
    const lv = autoLevels(new Float32Array(100).fill(-90), null);
    expect(lv.maxDb - lv.minDb).toBeGreaterThanOrEqual(30);
  });
  it("max-resampling never loses a one-bin signal", () => {
    const db = new Float32Array(2048).fill(-100);
    db[1337] = -20;
    const px = resampleMax(db, 300);
    expect(Math.max(...px)).toBe(-20);
    expect(px.filter((v) => v === -20).length).toBe(1);
    expect(resampleMax(Float32Array.from([1, 2]), 4).length).toBe(4);   // upsampling: nearest bin
  });
  it("colour map has 256 opaque entries from dark to white", () => {
    const lut = colormap();
    expect(lut.length).toBe(1024);
    expect(Array.from(lut.slice(0, 4))).toEqual([0, 0, 16, 255]);
    expect(Array.from(lut.slice(1020))).toEqual([255, 255, 255, 255]);
    expect(levelIndex(-200, { minDb: -100, maxDb: 0 })).toBe(0);
    expect(levelIndex(10, { minDb: -100, maxDb: 0 })).toBe(255);
  });
  it("maps pixels and integer Hz both ways and snaps to steps", () => {
    expect(pixelToHz(0, 1000, 10_000_000, 384000)).toBe(9_808_000);
    expect(pixelToHz(500, 1000, 10_000_000, 384000)).toBe(10_000_000);
    expect(hzToPixel(10_096_000, 1000, 10_000_000, 384000)).toBe(750);
    expect(snap(7_074_420, 1000)).toBe(7_074_000);
    expect(snap(7_074_500, 500)).toBe(7_074_500);
  });
  it("ticks fall on round frequencies inside the span", () => {
    const t = ticks(7_100_000, 384000);
    expect(t.length).toBeGreaterThanOrEqual(6);
    expect(t.every((x) => x.hz % 50_000 === 0 && x.hz >= 6_908_000 && x.hz <= 7_292_000)).toBe(true);
    expect(t[0]!.label).toBe("6.95");
  });
});

describe("zoom views", () => {
  const C = 7_074_000, R = 384_000;
  it("starts as the capture and zooms around the anchor, inside the capture", () => {
    const v = captureView(C, R);
    expect(v).toEqual({ loHz: C - R / 2, hiHz: C + R / 2 });
    const z = zoomView(v, 4, C + 48_000, C, R);
    expect(z.hiHz - z.loHz).toBe(R / 4);
    // the anchor stays at the same screen fraction (0.625 of the width)
    expect((C + 48_000 - z.loHz) / (z.hiHz - z.loHz)).toBeCloseTo(0.625, 9);
    expect(zoomOf(z, R)).toBe(4);
  });
  it("is limited to 1..MAX_ZOOM and never leaves the capture", () => {
    let v = captureView(C, R);
    for (let i = 0; i < 20; i++) v = zoomView(v, 2, C + R / 2, C, R);    // zoom in at the right edge
    expect(zoomOf(v, R)).toBe(MAX_ZOOM);
    expect(v.hiHz).toBe(C + R / 2);
    for (let i = 0; i < 20; i++) v = zoomView(v, 0.5, C, C, R);
    expect(v).toEqual(captureView(C, R));
    expect(clampView(C - R, R / 8, C, R).loHz).toBe(C - R / 2);
  });
});

describe("rowToPixels", () => {
  it("maps bins to the view, max per pixel, NaN outside the row's capture", () => {
    const db = Float32Array.from([0, 1, 2, 3, 4, 5, 6, 7]);   // 8 bins over 800 Hz around 1000 Hz
    const whole = rowToPixels(db, 1000, 800, { loHz: 600, hiHz: 1400 }, 4);
    expect(Array.from(whole)).toEqual([1, 3, 5, 7]);
    const zoomed = rowToPixels(db, 1000, 800, { loHz: 1000, hiHz: 1200 }, 4);   // bins 4,5 over 4 px
    expect(Array.from(zoomed)).toEqual([4, 4, 5, 5]);
    const shifted = rowToPixels(db, 1000, 800, { loHz: 1200, hiHz: 1600 }, 4);  // half outside
    expect(Array.from(shifted.slice(0, 2))).toEqual([6, 7]);
    expect(Number.isNaN(shifted[2]!) && Number.isNaN(shifted[3]!)).toBe(true);
  });
});

describe("wheelSteps", () => {
  it("one notch is one step; small trackpad deltas accumulate", () => {
    expect(wheelSteps(0, { deltaY: -100, deltaMode: 0 })).toEqual({ steps: 1, acc: 0 });
    expect(wheelSteps(0, { deltaY: 3, deltaMode: 1 })).toEqual({ steps: -1, acc: -20 });
    let acc = 0, steps = 0;
    for (let i = 0; i < 10; i++) { const r = wheelSteps(acc, { deltaY: -12, deltaMode: 0 }); acc = r.acc; steps += r.steps; }
    expect(steps).toBe(1);
    expect(acc).toBe(20);
  });
});
