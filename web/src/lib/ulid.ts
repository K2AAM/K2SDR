// Command ids: ULIDs (26 Crockford base32 chars: 48-bit ms time + 80 random bits).
const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

export function ulid(now: number = Date.now()): string {
  let t = "";
  let ms = now;
  for (let i = 0; i < 10; i++) {
    t = ALPHABET[ms % 32] + t;
    ms = Math.floor(ms / 32);
  }
  const rnd = new Uint8Array(16);
  crypto.getRandomValues(rnd);
  let r = "";
  for (let i = 0; i < 16; i++) r += ALPHABET[rnd[i]! % 32];
  return t + r;
}
