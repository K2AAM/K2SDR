"""Async client for one ksdr-worker process.

Owns the process and its two socketpairs. Commands are matched to replies by
cmd_seq. The caller (ReceiverRuntime) serializes commands; this class only
guarantees correct matching, timeouts and failure propagation.

A late APPLIED for a command whose wait already timed out is still reported
(on_late_applied): the worker's readback is the truth about the device, even
when the command itself was reported failed.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Callable, Protocol

from ..protocol import worker_ctl as wc
from ..protocol.frame import HEADER_LEN_V1, FrameHeader


class WorkerError(Exception):
    """Base class for worker command failures."""


class WorkerRejected(WorkerError):
    def __init__(self, result: wc.Result) -> None:
        super().__init__(result.name)
        self.result = result


class WorkerFailed(WorkerError):
    def __init__(self, result: wc.Result) -> None:
        super().__init__(result.name)
        self.result = result


class WorkerTimeout(WorkerError):
    pass


class WorkerGone(WorkerError):
    pass


class WorkerStartError(WorkerError):
    def __init__(self, result: wc.Result | None, exit_code: int | None, stderr: str) -> None:
        super().__init__(f"worker failed to start: result={result and result.name} exit={exit_code} {stderr[:200]}")
        self.result, self.exit_code, self.stderr = result, exit_code, stderr


@dataclass
class Callbacks:
    on_frame: Callable[[FrameHeader, bytes], None] = lambda h, p: None
    on_status: Callable[[wc.Status], None] = lambda s: None
    on_late_applied: Callable[[wc.Applied], None] = lambda a: None
    on_late_channel_applied: Callable[[wc.ChannelApplied], None] = lambda a: None
    on_late_hw_applied: Callable[[wc.HwApplied], None] = lambda a: None
    on_exit: Callable[[int], None] = lambda code: None


class WorkerLink(Protocol):
    """What ReceiverRuntime needs from a worker; FakeWorker implements it in tests."""

    async def start(self, capture_gen_start: int) -> None: ...
    async def set_capture(self, center_hz: int | None, rate_hz: int | None, timeout: float) -> wc.Applied: ...
    async def stop(self, timeout: float) -> None: ...
    def kill(self) -> None: ...


ArgvBuilder = Callable[[int, int, int], list[str]]   # (ctl_fd, data_fd, capture_gen_start) -> argv


class WorkerProcess:
    def __init__(self, argv_builder: ArgvBuilder, callbacks: Callbacks, *, start_timeout: float = 5.0) -> None:
        self._argv = argv_builder
        self._cb = callbacks
        self._start_timeout = start_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._ctl_w: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._seq = 0
        self._acks: dict[int, asyncio.Future] = {}
        self._dones: dict[int, asyncio.Future] = {}
        self._timed_out: set[int] = set()
        self._first_status: asyncio.Future | None = None
        self._exit_code: int | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._exit_code is None

    async def start(self, capture_gen_start: int) -> None:
        loop = asyncio.get_running_loop()
        ctl_p, ctl_c = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        data_p, data_c = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            argv = self._argv(ctl_c.fileno(), data_c.fileno(), capture_gen_start)
            self._proc = await asyncio.create_subprocess_exec(
                *argv, pass_fds=(ctl_c.fileno(), data_c.fileno()),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        finally:
            ctl_c.close()
            data_c.close()
        ctl_r, self._ctl_w = await asyncio.open_unix_connection(sock=ctl_p)
        data_r, data_w = await asyncio.open_unix_connection(sock=data_p, limit=1 << 22)
        self._data_w = data_w
        self._first_status = loop.create_future()
        self._tasks = [
            asyncio.create_task(self._read_ctl(ctl_r), name="worker-ctl"),
            asyncio.create_task(self._read_data(data_r), name="worker-data"),
            asyncio.create_task(self._watch_exit(), name="worker-exit"),
        ]
        try:
            st: wc.Status | None = await asyncio.wait_for(asyncio.shield(self._first_status), self._start_timeout)
        except asyncio.TimeoutError:
            self.kill()
            raise WorkerStartError(None, None, "timed out waiting for STATUS") from None
        if st is None or st.state is not wc.WorkerState.RUNNING:
            code = await self._proc.wait()
            err = (await self._proc.stderr.read()).decode(errors="replace") if self._proc.stderr else ""
            raise WorkerStartError(st.result if st else None, code, err)

    # ------------------------------------------------------------ commands
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _send(self, msg_type: wc.MsgType, payload: bytes = b"") -> int:
        if not self.running or self._ctl_w is None:
            raise WorkerGone("worker is not running")
        seq = self._next_seq()
        self._ctl_w.write(wc.CtlHeader(msg_type, len(payload), seq).pack() + payload)
        return seq

    async def _command(self, msg_type: wc.MsgType, payload: bytes, timeout: float):
        """ACK, then APPLIED (SET_CAPTURE/STOP), CHANNEL_APPLIED (CHANNEL_*) or HW_APPLIED (SET_HW),
        or FAILED."""
        loop = asyncio.get_running_loop()
        seq = self._send(msg_type, payload)
        ack, done = loop.create_future(), loop.create_future()
        self._acks[seq], self._dones[seq] = ack, done
        await self._ctl_w.drain()
        try:
            async with asyncio.timeout(timeout):
                result: wc.Result = await ack
                if result is not wc.Result.OK:
                    self._dones.pop(seq, None)
                    raise WorkerRejected(result)
                return await done
        except TimeoutError:
            self._timed_out.add(seq)
            raise WorkerTimeout(f"no reply to {msg_type.name} within {timeout}s") from None
        finally:
            self._acks.pop(seq, None)

    async def set_capture(self, center_hz: int | None, rate_hz: int | None, timeout: float) -> wc.Applied:
        return await self._command(wc.MsgType.SET_CAPTURE, wc.SetCapture(center_hz, rate_hz).pack_payload(), timeout)

    async def set_hw(self, hw: wc.SetHw, timeout: float = 5.0) -> wc.HwApplied:
        return await self._command(wc.MsgType.SET_HW, hw.pack_payload(), timeout)

    async def channel_open(self, ch: wc.Channel, timeout: float = 5.0) -> wc.ChannelApplied:
        return await self._command(wc.MsgType.CHANNEL_OPEN, ch.pack_payload(), timeout)

    async def channel_tune(self, ch: wc.Channel, timeout: float = 5.0) -> wc.ChannelApplied:
        return await self._command(wc.MsgType.CHANNEL_TUNE, ch.pack_payload(), timeout)

    async def channel_close(self, channel_id: int, timeout: float = 5.0) -> wc.ChannelApplied:
        return await self._command(wc.MsgType.CHANNEL_CLOSE, wc.pack_channel_close(channel_id), timeout)

    async def stop(self, timeout: float = 5.0) -> None:
        if not self.running:
            return
        try:
            await self._command(wc.MsgType.STOP, b"", timeout)
            await asyncio.wait_for(self._proc.wait(), timeout)
        except (WorkerError, asyncio.TimeoutError):
            self.kill()
        finally:
            await self._finish_tasks()

    def kill(self) -> None:
        if self._proc and self._exit_code is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    async def _finish_tasks(self) -> None:
        if self._proc:
            await self._proc.wait()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------- readers
    async def _read_ctl(self, r: asyncio.StreamReader) -> None:
        try:
            while True:
                hdr = wc.CtlHeader.unpack(await r.readexactly(wc.HEADER_LEN_V1))
                payload = await r.readexactly(hdr.payload_len) if hdr.payload_len else b""
                self._dispatch(hdr, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except wc.CtlError:
            # A malformed message from the worker is a worker bug; stop trusting it.
            self.kill()
        finally:
            if self._first_status and not self._first_status.done():
                self._first_status.set_result(None)

    def _dispatch(self, hdr: wc.CtlHeader, payload: bytes) -> None:
        t = hdr.msg_type
        if t is wc.MsgType.STATUS:
            st = wc.Status.unpack_payload(payload)
            if self._first_status and not self._first_status.done():
                self._first_status.set_result(st)
            self._cb.on_status(st)
        elif t is wc.MsgType.ACK:
            fut = self._acks.get(hdr.cmd_seq)
            if fut and not fut.done():
                fut.set_result(wc.unpack_result(t, payload))
        elif t in (wc.MsgType.APPLIED, wc.MsgType.CHANNEL_APPLIED, wc.MsgType.HW_APPLIED):
            ap = {wc.MsgType.APPLIED: wc.Applied, wc.MsgType.CHANNEL_APPLIED: wc.ChannelApplied,
                  wc.MsgType.HW_APPLIED: wc.HwApplied}[t].unpack_payload(payload)
            fut = self._dones.pop(hdr.cmd_seq, None)
            if fut and not fut.done():
                fut.set_result(ap)
            elif hdr.cmd_seq in self._timed_out:
                self._timed_out.discard(hdr.cmd_seq)
                if isinstance(ap, wc.ChannelApplied):
                    self._cb.on_late_channel_applied(ap)
                elif isinstance(ap, wc.HwApplied):
                    self._cb.on_late_hw_applied(ap)
                else:
                    self._cb.on_late_applied(ap)
        elif t is wc.MsgType.FAILED:
            res = wc.unpack_result(t, payload)
            fut = self._dones.pop(hdr.cmd_seq, None)
            if fut and not fut.done():
                fut.set_exception(WorkerFailed(res))
            self._timed_out.discard(hdr.cmd_seq)
        # PONG: no caller waits on it in M0.

    async def _read_data(self, r: asyncio.StreamReader) -> None:
        try:
            while True:
                hdr = FrameHeader.unpack(await r.readexactly(HEADER_LEN_V1))
                if hdr.header_len > HEADER_LEN_V1:
                    hdr = hdr.with_ext(await r.readexactly(hdr.header_len - HEADER_LEN_V1))
                payload = await r.readexactly(hdr.payload_len) if hdr.payload_len else b""
                self._cb.on_frame(hdr, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _watch_exit(self) -> None:
        code = await self._proc.wait()
        self._exit_code = code
        for fut in list(self._acks.values()) + list(self._dones.values()):
            if not fut.done():
                fut.set_exception(WorkerGone(f"worker exited with {code}"))
        self._acks.clear()
        self._dones.clear()
        if self._ctl_w:
            self._ctl_w.close()
        if getattr(self, "_data_w", None):
            self._data_w.close()
        self._cb.on_exit(code)

