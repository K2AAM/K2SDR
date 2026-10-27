"""Synchronous harness for driving ksdr-worker in tests.

Spawns the worker with two socketpairs (control, data), exactly as the
control service will, and speaks frame_v1 / worker_ctl_v1 through the Python
mirrors (themselves ABI-checked against the C headers).
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.frame import HEADER_LEN_V1, FrameHeader, FrameKind
from k2websdr.replay.sigmf import Recording
from k2websdr.replay.worker_argv import WorkerIdentity, replay_argv

ROOT = Path(__file__).resolve().parents[2]
WORKER_BIN = Path(os.environ.get("K2_WORKER_BIN", ROOT / "build/worker/ksdr-worker"))


def _recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"wanted {n} bytes, got {len(buf)}")
        r, _, _ = select.select([sock], [], [], left)
        if not r:
            continue
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"socket closed after {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


@dataclass
class Frame:
    header: FrameHeader
    payload: bytes


class WorkerProc:
    def __init__(self, rec: Recording, ident: WorkerIdentity, *, frame_samples: int = 4096,
                 paced: bool = False, loop: bool = False, extra_args: list[str] | None = None,
                 capture_gen_start: int = 1,
                 expect_running: bool = True) -> None:
        if not WORKER_BIN.is_file():
            raise FileNotFoundError(f"{WORKER_BIN} not built; run: make worker")
        self.ctl, ctl_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.data, data_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        argv = replay_argv(WORKER_BIN, rec, ident, ctl_fd=ctl_child.fileno(), data_fd=data_child.fileno(),
                           frame_samples=frame_samples, paced=paced, loop=loop,
                           capture_gen_start=capture_gen_start) + (extra_args or [])
        self.proc = subprocess.Popen(argv, pass_fds=(ctl_child.fileno(), data_child.fileno()),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        ctl_child.close()
        data_child.close()
        self._seq = 0
        if expect_running:
            hdr, payload = self.read_ctl()
            assert hdr.msg_type is wc.MsgType.STATUS, hdr
            st = wc.Status.unpack_payload(payload)
            assert st.state is wc.WorkerState.RUNNING, st

    # ---------------------------------------------------------------- data
    def read_frame(self, timeout: float = 5.0) -> Frame:
        raw = _recv_exact(self.data, HEADER_LEN_V1, timeout)
        hdr = FrameHeader.unpack(raw)
        if hdr.header_len > HEADER_LEN_V1:
            hdr = hdr.with_ext(_recv_exact(self.data, hdr.header_len - HEADER_LEN_V1, timeout))
        payload = _recv_exact(self.data, hdr.payload_len, timeout) if hdr.payload_len else b""
        return Frame(hdr, payload)

    def read_until_eos(self, timeout: float = 10.0) -> list[Frame]:
        out = []
        deadline = time.monotonic() + timeout
        while True:
            f = self.read_frame(max(0.1, deadline - time.monotonic()))
            out.append(f)
            if f.header.kind is FrameKind.END_OF_STREAM:
                return out

    # ------------------------------------------------------------- control
    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send(self, msg_type: wc.MsgType, payload: bytes = b"", seq: int | None = None) -> int:
        seq = self.next_seq() if seq is None else seq
        self.ctl.sendall(wc.CtlHeader(msg_type, len(payload), seq).pack() + payload)
        return seq

    def read_ctl(self, timeout: float = 5.0) -> tuple[wc.CtlHeader, bytes]:
        hdr = wc.CtlHeader.unpack(_recv_exact(self.ctl, wc.HEADER_LEN_V1, timeout))
        payload = _recv_exact(self.ctl, hdr.payload_len, timeout) if hdr.payload_len else b""
        return hdr, payload

    def set_capture(self, center_hz: int | None = None, rate_hz: int | None = None) -> int:
        return self.send(wc.MsgType.SET_CAPTURE, wc.SetCapture(center_hz, rate_hz).pack_payload())

    def channel(self, msg: wc.MsgType, ch: wc.Channel | int) -> tuple[wc.Result, wc.ChannelApplied | None]:
        """Send CHANNEL_OPEN/TUNE (a Channel) or CHANNEL_CLOSE (an id); return (ack result, applied)."""
        payload = wc.pack_channel_close(ch) if msg is wc.MsgType.CHANNEL_CLOSE else ch.pack_payload()
        seq = self.send(msg, payload)
        hdr, p = self.read_ctl()
        assert hdr.msg_type is wc.MsgType.ACK and hdr.cmd_seq == seq, hdr
        res = wc.unpack_result(wc.MsgType.ACK, p)
        if res is not wc.Result.OK:
            return res, None
        hdr, p = self.read_ctl()
        assert hdr.msg_type is wc.MsgType.CHANNEL_APPLIED and hdr.cmd_seq == seq, hdr
        return res, wc.ChannelApplied.unpack_payload(p)

    # ------------------------------------------------------------- process
    def wait(self, timeout: float = 5.0) -> int:
        return self.proc.wait(timeout)

    def stderr(self) -> str:
        return self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""

    def close(self) -> None:
        for s in (self.ctl, self.data):
            try:
                s.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)
        if self.proc.stderr:
            self.proc.stderr.close()

    def __enter__(self) -> "WorkerProc":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
