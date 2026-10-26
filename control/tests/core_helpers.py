"""Helpers for ControlCore tests: async runner, client mirror, command builders."""

from __future__ import annotations

import asyncio
import copy
import functools
import itertools
import json
from pathlib import Path

from k2websdr.core.receiver import ReceiverConfig
from k2websdr.core.service import ControlCore, Principal, Session
from k2websdr.core.store import apply_ops
from k2websdr.protocol import models as m
from k2websdr.replay import sigmf

ROOT = Path(__file__).resolve().parents[2]
REC = sigmf.load(ROOT / "fixtures/iq/two_tone_ci16.sigmf-meta", require_sha512=True)
CENTER, RATE = REC.freq_origin_hz, REC.sample_rate_hz

OPERATOR = Principal("user:adam", frozenset({"listen", "hw.retune", "hw.antenna"}))
LISTENER = Principal("user:guest", frozenset({"listen"}))

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ids = itertools.count(1)


def new_id() -> str:
    n = next(_ids)
    tail = ""
    for _ in range(10):
        tail = _ULID_ALPHABET[n % 32] + tail
        n //= 32
    return "01JBQ6Z9K3M5N7" + "PQ" + tail


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(asyncio.wait_for(fn(*a, **kw), 30))
    return wrapper


def cmd(op: str, **args) -> tuple[str, str]:
    cid = new_id()
    return cid, json.dumps({"v": 1, "type": "cmd", "id": cid, "op": op, "args": args})


def rx_config(rid: str = "replay-0", **kw) -> ReceiverConfig:
    return ReceiverConfig(receiver_id=rid, label=rid, receiver_idx=0, recording=REC, **kw)


class Client:
    """A test client: a session plus a state mirror built only from what the server sent."""

    def __init__(self, core: ControlCore, principal: Principal = OPERATOR) -> None:
        self.core = core
        self.session: Session = core.open_session(principal)
        self.mirror: dict | None = None
        self.rev: int | None = None
        self.log: list[m.BaseModel] = []
        self.history: list[dict] = []      # mirror after every applied patch

    def drain(self) -> list[m.BaseModel]:
        out = []
        while not self.session.outbox.empty():
            msg = self.session.outbox.get_nowait()
            out.append(msg)
            self._absorb(msg)
        self.log.extend(out)
        return out

    def _absorb(self, msg) -> None:
        if isinstance(msg, m.Snapshot):
            self.mirror = json.loads(msg.state.model_dump_json())
            self.rev = msg.rev
            self.history.append(copy.deepcopy(self.mirror))
        elif isinstance(msg, m.Patch):
            assert self.mirror is not None, "patch before snapshot"
            assert msg.base == self.rev, f"patch base {msg.base} != mirror rev {self.rev}"
            apply_ops(self.mirror, [json.loads(op.model_dump_json()) for op in msg.ops])
            self.rev = msg.rev
            self.history.append(copy.deepcopy(self.mirror))

    async def send(self, op: str, **args) -> str:
        cid, raw = cmd(op, **args)
        await self.core.handle(self.session, raw)
        return cid

    async def send_raw(self, raw: str) -> None:
        await self.core.handle(self.session, raw)

    def replies(self, cid: str) -> list[m.BaseModel]:
        self.drain()
        return [x for x in self.log if isinstance(x, (m.Ack, m.Result)) and x.id == cid]

    async def outcome(self, cid: str, timeout: float = 10.0) -> tuple[m.Ack, m.Result | None]:
        """Wait until the command has its result (or was rejected)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            rs = self.replies(cid)
            acks = [r for r in rs if isinstance(r, m.Ack)]
            results = [r for r in rs if isinstance(r, m.Result)]
            if acks and (acks[0].status == "rejected" or results):
                return acks[0], (results[0] if results else None)
            if loop.time() > end:
                raise TimeoutError(f"no outcome for {cid}: {rs}")
            await asyncio.sleep(0.01)

    async def subscribe(self) -> None:
        cid = await self.send("state.subscribe")
        await self.outcome(cid)

    def field(self, rid: str, name: str) -> dict:
        self.drain()
        return self.mirror["receivers"][rid]["capture"][name]
