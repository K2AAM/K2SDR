import { expect, it } from "vitest";
import { ulid } from "./ulid";

it("makes ids the server accepts (CmdId: 26 Crockford base32 chars), time-ordered", () => {
  const a = ulid(1_700_000_000_000), b = ulid(1_700_000_000_001);
  expect(a).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
  expect(a.slice(0, 10) < b.slice(0, 10)).toBe(true);
  expect(new Set(Array.from({ length: 100 }, () => ulid())).size).toBe(100);
});
