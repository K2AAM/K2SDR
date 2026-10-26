"""Revisioned server state with snapshot/patch delivery.

The server is the single writer. State lives as a JSON-compatible dict;
every committed transaction is re-validated against the ServerState model
(so invariants such as "a pending field references its command" can never
be published), bumps the revision by exactly one, and yields one Patch whose
ops reproduce the change on any client holding the previous revision.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..protocol import models as m


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer(*parts: str | int) -> str:
    return "".join("/" + _escape(str(p)) for p in parts)


def _parse(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"bad JSON pointer {path!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def apply_ops(doc: dict, ops: list[dict]) -> None:
    """Apply RFC 6902 add/replace/remove (object members only) in place."""
    for op in ops:
        *parents, last = _parse(op["path"])
        node = doc
        for p in parents:
            node = node[p]
        if op["op"] == "remove":
            del node[last]
        elif op["op"] == "replace":
            if last not in node:
                raise KeyError(f"replace of missing {op['path']}")
            node[last] = copy.deepcopy(op["value"])
        elif op["op"] == "add":
            node[last] = copy.deepcopy(op["value"])
        else:
            raise ValueError(op["op"])


class Txn:
    def __init__(self, doc: dict) -> None:
        self._doc = doc
        self.ops: list[dict] = []

    def get(self, *parts: str | int) -> Any:
        node: Any = self._doc
        for p in parts:
            node = node[str(p)] if isinstance(node, dict) else node[p]
        return node

    def has(self, *parts: str | int) -> bool:
        try:
            self.get(*parts)
            return True
        except (KeyError, IndexError, TypeError):
            return False

    def replace(self, *parts: str | int, value: Any) -> None:
        op = {"op": "replace", "path": pointer(*parts), "value": _jsonable(value)}
        apply_ops(self._doc, [op])
        self.ops.append(op)

    def add(self, *parts: str | int, value: Any) -> None:
        op = {"op": "add", "path": pointer(*parts), "value": _jsonable(value)}
        apply_ops(self._doc, [op])
        self.ops.append(op)

    def remove(self, *parts: str | int) -> None:
        op = {"op": "remove", "path": pointer(*parts)}
        apply_ops(self._doc, [op])
        self.ops.append(op)


def _jsonable(value: Any) -> Any:
    if isinstance(value, m.BaseModel):
        return json.loads(value.model_dump_json())
    return json.loads(json.dumps(value))


class StateStore:
    def __init__(self, initial: m.ServerState) -> None:
        self._doc: dict = json.loads(initial.model_dump_json())
        self._rev = 0
        self._listeners: list[Callable[[m.Patch], None]] = []

    @property
    def rev(self) -> int:
        return self._rev

    def view(self) -> dict:
        """A deep copy of the current state document (for reads and tests)."""
        return copy.deepcopy(self._doc)

    def snapshot(self) -> m.Snapshot:
        state = m.ServerState.model_validate_json(json.dumps(self._doc))
        return m.Snapshot(v=1, type="snapshot", rev=self._rev, state=state)

    def subscribe(self, fn: Callable[[m.Patch], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    @contextmanager
    def txn(self) -> Iterator[Txn]:
        """All changes in the block become one revision, or none if anything fails."""
        work = copy.deepcopy(self._doc)
        t = Txn(work)
        yield t
        if not t.ops:
            return
        # Invariants are enforced on every commit; a violation is a server bug.
        m.ServerState.model_validate_json(json.dumps(work))
        patch = m.Patch(v=1, type="patch", base=self._rev, rev=self._rev + 1,
                        ops=[m.PatchOp.model_validate(op) for op in t.ops])
        self._doc = work
        self._rev += 1
        for fn in list(self._listeners):
            fn(patch)
