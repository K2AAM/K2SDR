"""A1: wire frequencies are JSON integers in *_hz fields only; strict schema rejection.

Also covers the command/state message invariants the later lifecycle tests
(block 4) rely on: ack/result code rules, consecutive patches, field-state rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from k2websdr.protocol import models as m
from k2websdr.protocol.errors import ErrorCode
from k2websdr.protocol.models import ProtocolError, parse_client_message
from k2websdr.protocol.schema import build_schema

CMD_ID = "01JBQ6Z9K3M5N7P8Q9R0S1T2V3"


def set_capture(**args) -> str:
    base = {"receiver": "replay-0"}
    base.update(args)
    return json.dumps({"v": 1, "type": "cmd", "id": CMD_ID, "op": "receiver.set_capture", "args": base})


def expect_error(raw, code: ErrorCode) -> ProtocolError:
    with pytest.raises(ProtocolError) as ei:
        parse_client_message(raw)
    assert ei.value.code is code, ei.value.detail
    return ei.value


# ---------------------------------------------------------------- valid input

def test_valid_set_capture():
    msg = parse_client_message(set_capture(center_hz=7_074_000, sample_rate_hz=768_000, if_capture_gen=12))
    assert isinstance(msg, m.ReceiverSetCapture)
    assert msg.args.center_hz == 7_074_000 and type(msg.args.center_hz) is int
    assert msg.args.sample_rate_hz == 768_000


def test_valid_channel_open_and_bytes_input():
    raw = json.dumps({"v": 1, "type": "cmd", "id": CMD_ID, "op": "channel.open",
                      "args": {"receiver": "replay-0", "frequency_hz": 7_074_000, "mode": "usb"}}).encode()
    msg = parse_client_message(raw)
    assert isinstance(msg, m.ChannelOpen) and msg.args.frequency_hz == 7_074_000


def test_hz_bounds_inclusive():
    assert parse_client_message(set_capture(center_hz=0)).args.center_hz == 0
    assert parse_client_message(set_capture(center_hz=m.MAX_HZ)).args.center_hz == m.MAX_HZ


# ------------------------------------------------- frequencies: integers only

@pytest.mark.parametrize("bad", [
    7074000.0,            # integral float
    7074000.5,
    "7074000",            # numeric string
    "7.074 MHz",          # unit string never allowed on the wire
    True,                 # bool is not an int here
    -1,
    m.MAX_HZ + 1,
    2**64,
    None,                 # center_hz null alone -> nothing to change
    [7074000],
    {"hz": 7074000},
])
def test_center_hz_rejects_non_integers(bad):
    expect_error(set_capture(center_hz=bad), ErrorCode.E_SCHEMA)


def test_float_literal_in_raw_json_is_rejected():
    raw = ('{"v":1,"type":"cmd","id":"%s","op":"receiver.set_capture",'
           '"args":{"receiver":"replay-0","center_hz":7.074e6}}' % CMD_ID)
    expect_error(raw, ErrorCode.E_SCHEMA)


@pytest.mark.parametrize("bad", [0, -768000, 2**32, 768000.0, "768000"])
def test_sample_rate_bounds(bad):
    expect_error(set_capture(sample_rate_hz=bad), ErrorCode.E_SCHEMA)


@pytest.mark.parametrize("field", ["center_khz", "center_mhz", "frequency", "center", "freq"])
def test_unit_named_or_unsuffixed_fields_are_unknown(field):
    expect_error(set_capture(**{field: 7074}), ErrorCode.E_SCHEMA)


def _walk_properties(schema, path=""):
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            yield f"{path}.{name}", name, sub
        for k, v in schema.items():
            yield from _walk_properties(v, f"{path}/{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _walk_properties(v, f"{path}[{i}]")


def _integer_only(sub, defs) -> bool:
    """True if every non-null branch of this property schema is an integer (or array/tuple of them)."""
    if "$ref" in sub:
        return _integer_only(defs[sub["$ref"].split("/")[-1]], defs)
    if "anyOf" in sub:
        return all(_integer_only(b, defs) for b in sub["anyOf"] if b.get("type") != "null")
    t = sub.get("type")
    if t == "integer":
        return True
    if t == "object" and sub.get("title", "").startswith("FieldState"):
        props = sub["properties"]
        return _integer_only(props["requested"], defs) and _integer_only(props["applied"], defs)
    if t == "array":
        items = sub.get("prefixItems") or [sub.get("items")]
        return all(i is not None and _integer_only(i, defs) for i in items)
    return False


def test_schema_every_hz_field_is_integer_and_every_frequency_field_ends_in_hz():
    schema = build_schema()
    defs = schema["$defs"]
    hz_fields = 0
    for path, name, sub in _walk_properties(schema):
        lname = name.lower()
        if lname.endswith("_hz"):
            hz_fields += 1
            assert _integer_only(sub, defs), f"{path} must be integer-only"
        elif "freq" in lname or "center" in lname or "rate" in lname:
            pytest.fail(f"{path}: frequency-like field without _hz suffix")
    assert hz_fields >= 6  # guards against the walk silently finding nothing


# -------------------------------------------------------- strict envelope

def test_unknown_envelope_field():
    obj = json.loads(set_capture(center_hz=1))
    obj["extra"] = 1
    expect_error(json.dumps(obj), ErrorCode.E_SCHEMA)


def test_duplicate_keys_rejected():
    raw = ('{"v":1,"type":"cmd","id":"%s","op":"receiver.set_capture",'
           '"args":{"receiver":"replay-0","center_hz":7074000,"center_hz":14074000}}' % CMD_ID)
    expect_error(raw, ErrorCode.E_SCHEMA)


@pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
def test_non_json_constants_rejected(const):
    raw = ('{"v":1,"type":"cmd","id":"%s","op":"receiver.set_capture",'
           '"args":{"receiver":"replay-0","center_hz":%s}}' % (CMD_ID, const))
    expect_error(raw, ErrorCode.E_SCHEMA)


@pytest.mark.parametrize("raw", ["", "null", "[]", "7074000", '"cmd"', "{", b"\xff\xfe"])
def test_non_object_or_invalid_json(raw):
    expect_error(raw, ErrorCode.E_SCHEMA)


def test_deep_nesting_is_a_schema_error_not_a_crash():
    expect_error("[" * 50_000 + "]" * 50_000, ErrorCode.E_PAYLOAD_TOO_LARGE)
    expect_error("[" * 20_000 + "]" * 20_000, ErrorCode.E_SCHEMA)


def test_oversize_message():
    obj = json.loads(set_capture(center_hz=1))
    obj["args"]["receiver"] = "a" * (m.MAX_MESSAGE_BYTES + 1)
    expect_error(json.dumps(obj), ErrorCode.E_PAYLOAD_TOO_LARGE)


def test_unsupported_version_keeps_command_id():
    obj = json.loads(set_capture(center_hz=1))
    obj["v"] = 2
    err = expect_error(json.dumps(obj), ErrorCode.E_UNSUPPORTED_VERSION)
    assert err.cmd_id == CMD_ID


@pytest.mark.parametrize("bad_id", ["", "123", "01JBQ6Z9K3M5N7P8Q9R0S1T2VU", "01jbq6z9k3m5n7p8q9r0s1t2v3", 5])
def test_command_id_must_be_ulid(bad_id):
    obj = json.loads(set_capture(center_hz=1))
    obj["id"] = bad_id
    expect_error(json.dumps(obj), ErrorCode.E_SCHEMA)


def test_unknown_op():
    obj = json.loads(set_capture(center_hz=1))
    obj["op"] = "receiver.transmit"
    expect_error(json.dumps(obj), ErrorCode.E_SCHEMA)


def test_set_capture_must_change_something():
    expect_error(set_capture(), ErrorCode.E_SCHEMA)


def test_channel_tune_must_change_something():
    raw = json.dumps({"v": 1, "type": "cmd", "id": CMD_ID, "op": "channel.tune",
                      "args": {"channel": "ch_abc"}})
    expect_error(raw, ErrorCode.E_SCHEMA)


def test_every_op_has_a_permission_entry():
    union, _field = get_args(m.ClientMessage)
    ops = {get_args(cls.model_fields["op"].annotation)[0] for cls in get_args(union)}
    assert ops == set(m.OP_PERMISSION)


# --------------------------------------------------- server message rules

def test_ack_code_rules():
    m.Ack(v=1, type="ack", id=CMD_ID, status="accepted", rev=3)
    m.Ack(v=1, type="ack", id=CMD_ID, status="rejected", code=ErrorCode.E_NOT_LEASE_HOLDER)
    with pytest.raises(ValidationError):
        m.Ack(v=1, type="ack", id=CMD_ID, status="rejected")
    with pytest.raises(ValidationError):
        m.Ack(v=1, type="ack", id=CMD_ID, status="accepted", code=ErrorCode.E_BUSY)


def test_result_code_rules():
    m.Result(v=1, type="result", id=CMD_ID, status="applied", rev=4, capture_gen=13)
    with pytest.raises(ValidationError):
        m.Result(v=1, type="result", id=CMD_ID, status="failed", rev=4)
    with pytest.raises(ValidationError):
        m.Result(v=1, type="result", id=CMD_ID, status="applied", rev=4, code=ErrorCode.E_TIMEOUT)


def test_patch_must_be_consecutive_and_remove_has_no_value():
    p = m.Patch(v=1, type="patch", base=415, rev=416, ops=[
        m.PatchOp(op="replace", path="/receivers/replay-0/capture/center_hz/status", value="pending"),
        m.PatchOp(op="remove", path="/channels/ch_abc"),
    ])
    text = m.dump_server_message(p)
    assert json.loads(text)["ops"][1] == {"op": "remove", "path": "/channels/ch_abc"}
    with pytest.raises(ValidationError):
        m.Patch(v=1, type="patch", base=415, rev=417, ops=p.ops)
    with pytest.raises(ValidationError):
        m.PatchOp.model_validate({"op": "replace", "path": "/x"})
    with pytest.raises(ValidationError):
        m.PatchOp.model_validate({"op": "remove", "path": "/x", "value": 1})


def test_field_state_never_pending_without_command_and_never_unapplied_verified():
    with pytest.raises(ValidationError):
        m.FieldState[int](requested=7_074_000, status="pending")
    with pytest.raises(ValidationError):
        m.FieldState[int](status="failed", applied=7_000_000)
    with pytest.raises(ValidationError):
        m.FieldState[int](requested=7_074_000, verified="driver_readback")
    with pytest.raises(ValidationError):
        m.FieldState[int](requested=7_074_000.0)  # type: ignore[arg-type]
    ok = m.FieldState[int](requested=7_074_000, applied=7_000_000, status="pending", cmd=CMD_ID,
                           verified="driver_readback")
    assert ok.applied == 7_000_000  # the only value a UI may present as in effect


def test_lease_holder_shapes():
    m.LeaseHolder(kind="operator", principal="user:adam", session="s_abc")
    m.LeaseHolder(kind="job", principal="job:ale", job="job_ale-scan-1")
    with pytest.raises(ValidationError):
        m.LeaseHolder(kind="job", principal="job:ale", job="job_x", session="s_abc")
    with pytest.raises(ValidationError):
        m.LeaseHolder(kind="operator", principal="user:adam")


def _snapshot() -> m.Snapshot:
    fs_unsup = m.FieldState[str](status="unsupported")
    rx = m.ReceiverState(
        label="Replay 0", driver="replay",
        acquisition=m.Acquisition(state="running"),
        lease=m.Lease(holder=m.LeaseHolder(kind="job", principal="job:stub", job="job_stub-1"),
                      since_utc=datetime(2026, 10, 26, 17, 25, tzinfo=timezone.utc)),
        capture_gen=13,
        capture=m.Capture(
            center_hz=m.FieldState[int](requested=7_074_000, applied=7_074_000, status="applied",
                                        verified="driver_readback"),
            sample_rate_hz=m.FieldState[int](requested=768_000, applied=768_000, status="applied"),
            antenna=fs_unsup,
        ),
        capabilities=m.Capabilities(freq_range_hz=(0, 30_000_000), sample_rates_hz=[768_000],
                                    sample_format="ci16"),
    )
    ch = m.ChannelState(receiver="replay-0", owner=m.ChannelOwner(session="s_abc"),
                        frequency_hz=7_074_000, mode="usb", channel_gen=4)
    state = m.ServerState(server=m.ServerInfo(protocol=1, build="dev", exposure="loopback"),
                          receivers={"replay-0": rx}, channels={"ch_abc": ch},
                          ledger=m.LedgerSummary(version=1))
    return m.Snapshot(v=1, type="snapshot", rev=415, state=state)


def test_snapshot_round_trip_is_exact():
    snap = _snapshot()
    text = m.dump_server_message(snap)
    again = m.SERVER_ADAPTER.validate_json(text, strict=True)
    assert again == snap
    obj = json.loads(text)
    assert obj["state"]["receivers"]["replay-0"]["capture"]["center_hz"]["applied"] == 7_074_000
    assert isinstance(obj["state"]["channels"]["ch_abc"]["frequency_hz"], int)


def test_naive_timestamps_rejected():
    with pytest.raises(ValidationError):
        m.Lease(since_utc=datetime(2026, 10, 26, 17, 25))
