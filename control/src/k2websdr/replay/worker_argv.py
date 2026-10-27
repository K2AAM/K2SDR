"""Build ksdr-worker command lines from validated SigMF recordings.

The worker never parses JSON; everything it needs is passed explicitly here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..protocol.frame import SampleFormat
from .sigmf import Recording

_FORMAT_ARG = {
    SampleFormat.CI16: "ci16",
    SampleFormat.CF32: "cf32",
    SampleFormat.RI16: "ri16",
    SampleFormat.RF32: "rf32",
}
_DEVICE_ID = re.compile(r"^[a-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    device_id: str          # lock name; one physical device (or one replay "device") per id
    lock_dir: Path
    receiver_idx: int
    stream_id: int = 0

    def __post_init__(self) -> None:
        if not _DEVICE_ID.match(self.device_id) or self.device_id in (".", ".."):
            raise ValueError(f"invalid device_id {self.device_id!r}")
        if not 0 <= self.receiver_idx <= 0xFFFFFFFF or not 0 <= self.stream_id <= 0xFFFFFFFF:
            raise ValueError("receiver_idx/stream_id must fit in u32")


def replay_argv(worker_bin: str | Path, rec: Recording, ident: WorkerIdentity, *, ctl_fd: int, data_fd: int,
                frame_samples: int = 4096, paced: bool = False, loop: bool = False,
                capture_gen_start: int = 1, iq_frames: bool = True, spectrum_fft: int = 0,
                spectrum_fps: int = 15) -> list[str]:
    if not 1 <= frame_samples <= 1 << 20:
        raise ValueError("frame_samples out of range")
    if not 1 <= capture_gen_start <= 2**53 - 1:
        raise ValueError("capture_gen_start out of range")
    argv = [
        str(worker_bin),
        "--device-id", ident.device_id,
        "--lock-dir", str(ident.lock_dir),
        "--receiver-idx", str(ident.receiver_idx),
        "--stream-id", str(ident.stream_id),
        "--ctl-fd", str(ctl_fd),
        "--data-fd", str(data_fd),
        "--source", "replay",
        "--file", str(rec.data_path),
        "--format", _FORMAT_ARG[rec.format],
        "--rate-hz", str(rec.sample_rate_hz),
        "--origin-hz", str(rec.freq_origin_hz),
        "--frame-samples", str(frame_samples),
        "--capture-gen-start", str(capture_gen_start),
    ]
    for d in rec.discontinuities:
        argv += ["--discontinuity", f"{d.sample_index}:{d.gap_samples}" if d.gap_samples else str(d.sample_index)]
    if not iq_frames:
        argv += ["--iq-frames", "off"]
    if spectrum_fft:
        argv += ["--spectrum-fft", str(spectrum_fft), "--spectrum-fps", str(spectrum_fps)]
    if paced:
        argv.append("--paced")
    if loop:
        argv.append("--loop")
    return argv
