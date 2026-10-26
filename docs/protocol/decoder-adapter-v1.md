# Decoder adapter protocol v1 — notes

Model: `control/src/k2websdr/protocol/decoder.py` (`DecoderEvent`). Decoders
run as supervised processes (supervision arrives with the first real decoders
in M3) and write one JSON object per line.

## Event fields

| Field | Meaning |
| --- | --- |
| `receiver`, `channel` | Stream the event was decoded from |
| `capture_gen`, `channel_gen` | Generations the decoder's input belonged to |
| `sample_index` | Decimal string (may exceed 2^53) |
| `utc` | Timezone-aware timestamp |
| `mode` | Ledger mode id |
| `raw` | Decoder's raw text, ≤ 4096 chars, untrusted |
| `fields`, `confidence` | Normalized fields and engine-supplied confidence |
| `engine` | Engine name and version |

Lines over 16 KiB, unknown fields, naive timestamps and non-integer
generations are rejected.

## Generation gate (A7)

`k2websdr.decoders.gate.GenerationGate` admits an event only when both
generations equal the current ones. The capture generation is updated from
worker `APPLIED` readbacks; the channel generation from the control service's
own channel state. Dropped events are counted by reason (`stale_capture_gen`,
`future_channel_gen`, `unknown_channel`, ...). Generations can never move
backwards, including across worker restarts (the restarted worker is started
with `--capture-gen-start last + 1`).

Decoded text is stored verbatim and treated as untrusted display input; it
is escaped at render time, never interpreted.
