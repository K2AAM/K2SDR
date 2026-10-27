"""Listener modes the worker implements (k2dsp), and what each needs from a receiver.

Availability is decided in two places (outline: adapter + dependencies + hardware bandwidth):
- globally by the ledger (ledger/integrations.v1.json -> LedgerSummary.available), and
- per receiver here: the capture must be complex and fast enough for the mode's channel rate,
  and the channel's passband must fit inside the capture span.
The numbers mirror worker/src/dsp/channel.cpp (docs/design/M1-dsp.md); the worker re-checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol import worker_ctl as wc


@dataclass(frozen=True)
class WorkerMode:
    worker: wc.Mode
    channel_rate_hz: int      # complex rate inside the channel: the capture must be at least this
    pass_lo_hz: int           # default passband, relative to the tuned frequency
    pass_hi_hz: int


WORKER_MODES: dict[str, WorkerMode] = {
    "am": WorkerMode(wc.Mode.AM, 12000, -4500, 4500),
    "usb": WorkerMode(wc.Mode.USB, 12000, 300, 2700),
    "lsb": WorkerMode(wc.Mode.LSB, 12000, -2700, -300),
    "cw": WorkerMode(wc.Mode.CW, 12000, -250, 250),
    "nfm": WorkerMode(wc.Mode.NFM, 24000, -6000, 6000),
    "wfm": WorkerMode(wc.Mode.WFM, 192000, -90000, 90000),
}


def receiver_mode_problem(mode: str, sample_rate_hz: int | None, complex_iq: bool) -> str | None:
    """Why `mode` cannot run on a receiver with this capture, or None if it can."""
    wm = WORKER_MODES.get(mode)
    if wm is None:
        return f"mode {mode} has no worker implementation"
    if not complex_iq:
        return f"mode {mode} needs complex IQ; this receiver delivers real samples"
    if sample_rate_hz is None:
        return "the receiver's capture rate is not known yet"
    if sample_rate_hz < wm.channel_rate_hz:
        return f"mode {mode} needs a capture rate of at least {wm.channel_rate_hz} Hz (now {sample_rate_hz} Hz)"
    return None


def span_problem(mode: str, frequency_hz: int, center_hz: int, sample_rate_hz: int) -> str | None:
    """Why a channel at frequency_hz does not fit the capture span, or None."""
    wm = WORKER_MODES[mode]
    offset = frequency_hz - center_hz
    half = sample_rate_hz // 2
    if offset + wm.pass_lo_hz < -half or offset + wm.pass_hi_hz > half:
        lo, hi = center_hz - half - wm.pass_lo_hz, center_hz + half - wm.pass_hi_hz
        return f"{mode} at {frequency_hz} Hz does not fit the capture span; tune between {lo} and {hi} Hz"
    return None
