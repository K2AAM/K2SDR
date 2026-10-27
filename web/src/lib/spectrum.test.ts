import { describe, expect, it } from "vitest";
import { autoLevels, binsToDb, colormap, hzToPixel, levelIndex, percentile, pixelToHz, resampleMax, snap, ticks }
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
