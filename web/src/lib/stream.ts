// Browser stream v1 decoder (docs/protocol/stream-v1.md; Python twin: k2websdr.protocol.stream).
// Shared vectors: protocol/vectors/stream_v1.json.

export const MAGIC = 0x4253324b;
export const FIXED_LEN = 64;
export const AUDIO_RATE_HZ = 12000;
export const Kind = { SPECTRUM: 1, AUDIO: 2 } as const;
export const Flags = { NEW_GEN: 1, DISCONTINUITY: 2, SERVER_DROP: 4 } as const;

export interface SpectrumMsg {
  kind: 1; flags: number; gen: number; sampleIndex: number; frequencyHz: number; sampleRateHz: number;
  id: string; dbFloorMdb: number; dbStepMdb: number; blocks: number; bins: Uint8Array;
}
export interface AudioMsg {
  kind: 2; flags: number; gen: number; sampleIndex: number; frequencyHz: number; sampleRateHz: number;
  id: string; levelCdb: number; passLoHz: number; passHiHz: number; mode: number; agc: number; pcm: Int16Array;
}
export type StreamMsg = SpectrumMsg | AudioMsg;

export class StreamError extends Error {}

const utf8 = new TextDecoder("utf-8", { fatal: true });

function u64(v: DataView, off: number): number {
  const big = v.getBigUint64(off, true);
  if (big > BigInt(Number.MAX_SAFE_INTEGER)) throw new StreamError("u64 field exceeds 2^53");
  return Number(big);
}

function i64(v: DataView, off: number): number {
  const big = v.getBigInt64(off, true);
  if (big > BigInt(Number.MAX_SAFE_INTEGER) || big < -BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new StreamError("i64 field exceeds 2^53");
  }
  return Number(big);
}

export function decode(buf: ArrayBuffer): StreamMsg {
  if (buf.byteLength < FIXED_LEN) throw new StreamError("short message");
  const v = new DataView(buf);
  if (v.getUint32(0, true) !== MAGIC || v.getUint8(4) !== 1) throw new StreamError("bad magic or version");
  const kind = v.getUint8(5);
  const headerLen = v.getUint16(6, true);
  const flags = v.getUint32(8, true);
  const count = v.getUint32(12, true);
  const idLen = v.getUint8(60);
  if (kind !== 1 && kind !== 2) throw new StreamError(`unknown kind ${kind}`);
  if (flags & ~0x7) throw new StreamError("undefined flags");
  if (idLen < 1 || idLen > 64 || headerLen !== FIXED_LEN + ((idLen + 3) & ~3) || buf.byteLength < headerLen) {
    throw new StreamError("bad id length / header_len");
  }
  const bytes = new Uint8Array(buf);
  if (bytes[61] || bytes[62] || bytes[63] || bytes.subarray(FIXED_LEN + idLen, headerLen).some((b) => b !== 0)) {
    throw new StreamError("reserved bytes must be zero");
  }
  const id = utf8.decode(bytes.subarray(FIXED_LEN, FIXED_LEN + idLen));
  const common = {
    flags, gen: u64(v, 16), sampleIndex: u64(v, 24), frequencyHz: i64(v, 32), sampleRateHz: v.getUint32(40, true), id,
  };
  const payload = buf.byteLength - headerLen;
  if (kind === 1) {
    if (payload !== count || v.getUint32(56, true) !== 0) throw new StreamError("bad SPECTRUM payload");
    return { kind: 1, ...common, dbFloorMdb: v.getInt32(44, true), dbStepMdb: v.getUint32(48, true),
             blocks: v.getUint32(52, true), bins: new Uint8Array(buf, headerLen, count) };
  }
  if (payload !== 2 * count || common.sampleRateHz !== AUDIO_RATE_HZ) throw new StreamError("bad AUDIO payload");
  // Int16Array needs 2-byte alignment: header_len is a multiple of 4, so the view is aligned.
  return { kind: 2, ...common, levelCdb: v.getInt32(44, true), passLoHz: v.getInt32(48, true),
           passHiHz: v.getInt32(52, true), mode: v.getUint16(56, true), agc: v.getUint16(58, true),
           pcm: new Int16Array(buf, headerLen, count) };
}
