"""Generation gate for decoder events (outline §8, §13; acceptance A7).

An event passes only if its capture_gen equals the receiver's current
generation AND its channel_gen equals the channel's current generation.
Anything else was decoded from samples that belong to a previous frequency,
rate, antenna or channel tuning, and is dropped and counted.

The gate is updated from authoritative sources only: worker APPLIED/STATUS
for capture_gen, and the control service's own channel state for channel_gen.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..protocol.decoder import DecoderEvent


@dataclass
class GateStats:
    passed: int = 0
    dropped: Counter = field(default_factory=Counter)   # reason -> count

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())


class GenerationGate:
    def __init__(self) -> None:
        self._capture: dict[str, int] = {}
        self._channel: dict[str, tuple[str, int]] = {}   # channel -> (receiver, channel_gen)
        self.stats = GateStats()

    # ---- authoritative updates
    def set_capture_gen(self, receiver: str, gen: int) -> None:
        prev = self._capture.get(receiver)
        if prev is not None and gen < prev:
            raise ValueError(f"{receiver}: capture_gen went backwards ({prev} -> {gen})")
        self._capture[receiver] = gen

    def set_channel_gen(self, channel: str, receiver: str, gen: int) -> None:
        prev = self._channel.get(channel)
        if prev is not None and prev[0] == receiver and gen < prev[1]:
            raise ValueError(f"{channel}: channel_gen went backwards ({prev[1]} -> {gen})")
        self._channel[channel] = (receiver, gen)

    def remove_channel(self, channel: str) -> None:
        self._channel.pop(channel, None)

    def remove_receiver(self, receiver: str) -> None:
        self._capture.pop(receiver, None)
        for ch in [c for c, (r, _) in self._channel.items() if r == receiver]:
            del self._channel[ch]

    def capture_gen(self, receiver: str) -> int | None:
        return self._capture.get(receiver)

    # ---- the check
    def admit(self, ev: DecoderEvent) -> bool:
        reason = self._reject_reason(ev)
        if reason is None:
            self.stats.passed += 1
            return True
        self.stats.dropped[reason] += 1
        return False

    def _reject_reason(self, ev: DecoderEvent) -> str | None:
        cur = self._capture.get(ev.receiver)
        if cur is None:
            return "unknown_receiver"
        if ev.capture_gen != cur:
            return "stale_capture_gen" if ev.capture_gen < cur else "future_capture_gen"
        ch = self._channel.get(ev.channel)
        if ch is None:
            return "unknown_channel"
        if ch[0] != ev.receiver:
            return "channel_receiver_mismatch"
        if ev.channel_gen != ch[1]:
            return "stale_channel_gen" if ev.channel_gen < ch[1] else "future_channel_gen"
        return None
