import { describe, expect, it } from "vitest";
import { JitterBuffer } from "./jitter";

const pcm = (n: number, v = 1000) => new Int16Array(n).fill(v);

describe("JitterBuffer", () => {
  it("stays silent until the target is buffered, then plays", () => {
    const j = new JitterBuffer(12000, 48000, 250, 1000);
    const out = new Float32Array(128);
    j.push(pcm(2000));                                         // 167 ms < 250 ms
    j.render(out);
    expect(out.every((x) => x === 0)).toBe(true);
    j.push(pcm(1200));                                         // 267 ms
    j.render(out);
    expect(out.every((x) => Math.abs(x - 1000 / 32768) < 1e-6)).toBe(true);
    expect(j.stats().playing).toBe(true);
  });
  it("consumes input at inRate/outRate and resamples a ramp linearly", () => {
    const j = new JitterBuffer(12000, 48000, 0, 1000);
    const ramp = Int16Array.from({ length: 100 }, (_, i) => i * 100);
    j.push(ramp);
    const out = new Float32Array(8);
    j.render(out);
    const got = Array.from(out, (x) => Math.round(x * 32768));
    expect(got).toEqual([0, 25, 50, 75, 100, 125, 150, 175]);  // 4 output samples per input sample
    expect(j.stats().bufferedMs).toBe(Math.round((98 * 1000) / 12000));
  });
  it("an underrun counts, goes silent and rebuffers to the target", () => {
    const j = new JitterBuffer(12000, 48000, 100, 1000);
    j.push(pcm(1200));                                         // exactly 100 ms
    j.render(new Float32Array(4800));                          // 100 ms of output: runs dry
    expect(j.stats().underruns).toBe(1);
    expect(j.stats().playing).toBe(false);
    j.push(pcm(600));                                          // 50 ms: not enough yet
    const out = new Float32Array(64);
    j.render(out);
    expect(out.every((x) => x === 0)).toBe(true);
  });
  it("bounds latency: a burst beyond maxMs drops the oldest down to the target", () => {
    const j = new JitterBuffer(12000, 48000, 200, 500);
    j.push(pcm(6000, 1));                                      // 500 ms: at the bound
    j.push(pcm(1200, 2));                                      // +100 ms -> trimmed to 200 ms (newest kept)
    expect(j.stats().bufferedMs).toBe(200);
    expect(j.stats().dropsMs).toBe(400);
    const out = new Float32Array(16);
    j.render(out);
    expect(Math.round(out[0]! * 32768)).toBe(1);               // 100 ms of value 1 then 100 ms of value 2
  });
  it("flush drops everything (a new channel generation never plays old audio)", () => {
    const j = new JitterBuffer(12000, 48000, 0, 1000);
    j.push(pcm(1200, 7));
    j.flush();
    j.push(pcm(100, 9));
    const out = new Float32Array(4);
    j.render(out);
    expect(Math.round(out[0]! * 32768)).toBe(9);
  });
});
