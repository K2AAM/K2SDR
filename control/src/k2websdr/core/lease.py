"""Receiver leases: who may change a receiver's physical state (outline §11).

One lease per receiver. The holder is an operator session or a server job,
never both. Job leases are independent of every browser session, so closing
or suspending a browser never cancels a job (acceptance A4).

Takeover is explicit. A claim on a held receiver succeeds only when the
claimant asks for "preempt_with_notice" AND the receiver's configured policy
is "preempt_with_notice". Otherwise it is refused with E_BUSY and the holder
is untouched. A preempted holder is told why, and the change is visible in
state. Re-claiming a lease you already hold is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..protocol import models as m
from ..protocol.errors import ErrorCode


@dataclass(frozen=True)
class Holder:
    kind: str                  # "operator" | "job"
    principal: str             # e.g. "user:adam" or "job:ale-scan"
    session: str | None = None
    job: str | None = None

    def to_model(self) -> m.LeaseHolder:
        return m.LeaseHolder(kind=self.kind, principal=self.principal, session=self.session, job=self.job)

    @staticmethod
    def operator(principal: str, session: str) -> "Holder":
        return Holder("operator", principal, session=session)

    @staticmethod
    def for_job(principal: str, job: str) -> "Holder":
        return Holder("job", principal, job=job)


class LeaseError(Exception):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


PreemptHook = Callable[[Holder, Holder], None]   # (previous holder, new holder)


class Lease:
    def __init__(self, receiver: str, policy: m.TakeoverPolicy) -> None:
        self.receiver = receiver
        self.policy: m.TakeoverPolicy = policy
        self.holder: Holder | None = None
        self.since: datetime | None = None
        self._on_preempt: list[PreemptHook] = []

    def on_preempt(self, hook: PreemptHook) -> None:
        self._on_preempt.append(hook)

    def to_model(self) -> m.Lease:
        return m.Lease(holder=self.holder.to_model() if self.holder else None,
                       since_utc=self.since, takeover_policy=self.policy)

    def is_held_by(self, h: Holder) -> bool:
        return self.holder == h

    def claim(self, claimant: Holder, takeover: m.TakeoverPolicy = "deny") -> Holder | None:
        """Take the lease. Returns the preempted holder, if any. Raises LeaseError(E_BUSY)."""
        if self.holder is None or self.holder == claimant:
            if self.holder is None:
                self.holder, self.since = claimant, datetime.now(timezone.utc)
            return None
        if takeover != "preempt_with_notice" or self.policy != "preempt_with_notice":
            raise LeaseError(ErrorCode.E_BUSY,
                             f"{self.receiver} is held by {self.holder.principal}"
                             + ("" if self.policy == "preempt_with_notice" else "; receiver policy is deny"))
        previous = self.holder
        self.holder, self.since = claimant, datetime.now(timezone.utc)
        for hook in list(self._on_preempt):
            hook(previous, claimant)
        return previous

    def release(self, by: Holder) -> None:
        if self.holder != by:
            raise LeaseError(ErrorCode.E_NOT_LEASE_HOLDER, f"{self.receiver} is not held by {by.principal}")
        self.holder, self.since = None, None

    def release_session(self, session: str) -> bool:
        """Drop an operator lease when its session ends. Job leases are never touched."""
        if self.holder and self.holder.kind == "operator" and self.holder.session == session:
            self.holder, self.since = None, None
            return True
        return False

    def require(self, h: Holder) -> None:
        if self.holder != h:
            who = self.holder.principal if self.holder else "nobody"
            raise LeaseError(ErrorCode.E_NOT_LEASE_HOLDER, f"{self.receiver} lease is held by {who}")
