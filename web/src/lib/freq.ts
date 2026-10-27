// Human frequency input -> integer Hz. TypeScript twin of k2websdr.protocol.freq; both must pass
// protocol/vectors/frequency_input.json. Exact decimal-string arithmetic: no float is ever made,
// so "7.074 MHz" is exactly 7074000. Units: Hz, kHz, MHz only; "mHz" (millihertz) is refused.

export type FreqErrorCode =
  | "E_EMPTY" | "E_SYNTAX" | "E_UNIT_REQUIRED" | "E_UNKNOWN_UNIT" | "E_AMBIGUOUS_UNIT" | "E_SUB_HZ"
  | "E_OUT_OF_RANGE";

export class FrequencyParseError extends Error {
  constructor(readonly code: FreqErrorCode, message: string) {
    super(message);
  }
}

export const MAX_HZ = 10_000_000_000;
const MAX_INPUT_LEN = 32;
const SCALE: Record<string, number> = { hz: 0, khz: 3, mhz: 6 };   // power of ten
const RE = /^([0-9]+)(?:\.([0-9]+))?[ \t]*([A-Za-z]+)?$/;

function unitExp(unit: string): number {
  if (unit === "mHz") {
    throw new FrequencyParseError("E_AMBIGUOUS_UNIT", "'mHz' means millihertz; write MHz for megahertz");
  }
  const k = SCALE[unit.toLowerCase()];
  if (k === undefined) throw new FrequencyParseError("E_UNKNOWN_UNIT", `unknown unit '${unit}'; use Hz, kHz or MHz`);
  return k;
}

export function parseFrequency(text: string, opts: { defaultUnit?: string; maxHz?: number } = {}): number {
  const maxHz = opts.maxHz ?? MAX_HZ;
  const s = text.trim();
  if (!s) throw new FrequencyParseError("E_EMPTY", "empty frequency");
  if (s.length > MAX_INPUT_LEN) throw new FrequencyParseError("E_SYNTAX", "frequency input too long");
  const m = RE.exec(s);
  if (!m) throw new FrequencyParseError("E_SYNTAX", `cannot read '${text}'; expected e.g. '7.074 MHz' (no commas or signs)`);
  let unit = m[3];
  if (unit === undefined) {
    if (opts.defaultUnit === undefined) {
      throw new FrequencyParseError("E_UNIT_REQUIRED", `'${text}' has no unit; add Hz, kHz or MHz`);
    }
    unit = opts.defaultUnit;
  }
  const k = unitExp(unit);
  const frac = m[2] ?? "";
  const significant = frac.slice(0, k);
  if (/[^0]/.test(frac.slice(k))) throw new FrequencyParseError("E_SUB_HZ", `'${text}' is not a whole number of Hz`);
  const hz = BigInt(m[1]!) * 10n ** BigInt(k) + (k ? BigInt(significant.padEnd(k, "0")) : 0n);
  if (hz > BigInt(maxHz)) throw new FrequencyParseError("E_OUT_OF_RANGE", `${hz} Hz exceeds the ${maxHz} Hz limit`);
  return Number(hz);
}

/** Display: "7.074 000 MHz" style with explicit unit, exact (integer Hz in, string out). */
export function formatFrequency(hz: number): string {
  if (!Number.isSafeInteger(hz) || hz < 0) return "—";
  const s = String(hz).padStart(7, "0");
  const mhz = s.slice(0, -6);
  const khz = s.slice(-6, -3);
  const h = s.slice(-3);
  return `${Number(mhz)}.${khz} ${h} MHz`;
}
