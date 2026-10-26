"""Decoder adapter protocol v1: decoder process -> control service, JSON lines.

Every event names the stream it was decoded from and the capture/channel
generation the decoder believed was current. The control service drops events
whose generation is not the receiver's/channel's current one (outline §8:
reject output from a previous frequency/session after a retune).

Decoded radio text (`raw`, `fields`) is untrusted display input everywhere.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AwareDatetime, Field, ValidationError

from .models import ChannelId, Gen, ModeId, ReceiverId, Strict

MAX_EVENT_LINE_BYTES = 16 * 1024


class EngineInfo(Strict):
    name: Annotated[str, Field(max_length=64)]
    version: Annotated[str, Field(max_length=64)]


class DecoderEvent(Strict):
    v: Annotated[int, Field(strict=True, ge=1, le=1)]
    receiver: ReceiverId
    capture_gen: Gen
    channel: ChannelId
    channel_gen: Gen
    sample_index: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,19})$")]   # decimal string: may exceed 2^53
    utc: AwareDatetime
    mode: ModeId
    raw: Annotated[str, Field(max_length=4096)]
    fields: dict[Annotated[str, Field(max_length=64)], Any] = {}
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    engine: EngineInfo


class DecoderEventError(ValueError):
    pass


def parse_event_line(line: str | bytes) -> DecoderEvent:
    """Parse one JSON line from a decoder. Raises DecoderEventError only."""
    if len(line) > MAX_EVENT_LINE_BYTES:
        raise DecoderEventError("event line too long")
    try:
        return DecoderEvent.model_validate_json(line)
    except (ValueError, ValidationError) as e:
        raise DecoderEventError(str(e)[:200]) from None
