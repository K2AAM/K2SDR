import { describe, expect, it } from "vitest";
import vectors from "../../../protocol/vectors/frequency_input.json";
import { FrequencyParseError, formatFrequency, parseFrequency } from "./freq";

interface Case { input: string; hz?: number; error?: string; default_unit?: string; max_hz?: number }

describe("protocol/vectors/frequency_input.json (shared with the Python parser)", () => {
  for (const c of vectors.cases as Case[]) {
    it(JSON.stringify(c.input) + (c.default_unit ? ` [default ${c.default_unit}]` : ""), () => {
      const run = () => parseFrequency(c.input, { defaultUnit: c.default_unit, maxHz: c.max_hz ?? vectors.max_hz });
      if (c.error) {
        try {
          run();
          expect.unreachable("parsed but should fail");
        } catch (e) {
          expect(e).toBeInstanceOf(FrequencyParseError);
          expect((e as FrequencyParseError).code).toBe(c.error);
        }
      } else {
        expect(run()).toBe(c.hz);
      }
    });
  }
});

describe("formatFrequency", () => {
  it("shows integer Hz with an explicit unit, exactly", () => {
    expect(formatFrequency(7074000)).toBe("7.074 000 MHz");
    expect(formatFrequency(10000000000)).toBe("10000.000 000 MHz");
    expect(formatFrequency(500)).toBe("0.000 500 MHz");
    expect(formatFrequency(14074123)).toBe("14.074 123 MHz");
  });
});
