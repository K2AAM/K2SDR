import { describe, expect, it } from "vitest";
import vectors from "../../../protocol/vectors/stream_v1.json";
import { decode, StreamError } from "./stream";

function buf(hex: string): ArrayBuffer {
  const b = new Uint8Array(hex.length / 2);
  for (let i = 0; i < b.length; i++) b[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return b.buffer;
}

describe("protocol/vectors/stream_v1.json (generated from the Python codec)", () => {
  for (const c of vectors.valid) {
    it(`decodes ${c.name}`, () => {
      const m = decode(buf(c.hex));
      const e = c.expect as Record<string, unknown>;
      expect([m.kind, m.flags, m.gen, m.sampleIndex, m.frequencyHz, m.sampleRateHz, m.id]).toEqual(
        [e.kind, e.flags, e.gen, e.sample_index, e.frequency_hz, e.sample_rate_hz, e.id]);
      if (m.kind === 1) {
        expect([m.dbFloorMdb, m.dbStepMdb, m.blocks, Array.from(m.bins)]).toEqual(
          [e.db_floor_mdb, e.db_step_mdb, e.blocks, e.bins]);
      } else {
        expect([m.levelCdb, m.passLoHz, m.passHiHz, m.mode, m.agc, Array.from(m.pcm)]).toEqual(
          [e.level_cdb, e.pass_lo_hz, e.pass_hi_hz, e.mode, e.agc, e.pcm]);
      }
    });
  }
  for (const c of vectors.invalid) {
    it(`refuses ${c.name}`, () => {
      expect(() => decode(buf(c.hex))).toThrow(StreamError);
    });
  }
});
