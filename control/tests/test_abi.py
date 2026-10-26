"""The Python mirrors must match protocol/frame_v1.h and protocol/worker_ctl_v1.h exactly.

Compiles a probe against the canonical headers (as C11 and as C++17, since the
worker is C++), then compares sizes, offsets, constants and a packed sample
against the Python side. A C compiler is a required dev dependency; this test
fails, rather than skips, when one is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from k2websdr.protocol import frame, worker_ctl as wc

PROBE = r"""
#include <stdio.h>
#include <string.h>
#include "frame_v1.h"
#include "worker_ctl_v1.h"

#define OFF(T, f) printf("\"%s\":%zu,", #f, offsetof(struct T, f))

static void hex(const void *p, size_t n) {
    const unsigned char *b = (const unsigned char *)p;
    putchar('"');
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    putchar('"');
}

int main(void) {
    struct ksdr_frame_header_v1 h;
    memset(&h, 0, sizeof h);
    h.magic = KSDR_FRAME_MAGIC; h.version = KSDR_FRAME_VERSION; h.header_len = KSDR_FRAME_HEADER_LEN_V1;
    h.kind = KSDR_KIND_SAMPLES; h.format = KSDR_FMT_RI16;
    h.flags = KSDR_F_DISCONTINUITY | KSDR_F_GAP_KNOWN | KSDR_F_NEW_GEN;
    h.capture_gen = 13; h.seq = 0x0102030405060708ull; h.first_sample_index = 123456789012ull;
    h.freq_origin_hz = -1; h.host_time_ns = 1793035503000000000ull; h.gap_samples = 4096;
    h.receiver_idx = 2; h.stream_id = 7; h.sample_rate_hz = 64000000u; h.sample_count = 16384;

    struct kctl_header_v1 ch; memset(&ch, 0, sizeof ch);
    ch.magic = KCTL_MAGIC; ch.version = KCTL_VERSION; ch.header_len = KCTL_HEADER_LEN_V1;
    ch.msg_type = KCTL_MSG_SET_CAPTURE; ch.payload_len = sizeof(struct kctl_set_capture_v1); ch.cmd_seq = 42;
    struct kctl_set_capture_v1 sc; memset(&sc, 0, sizeof sc);
    sc.center_hz = 7074000; sc.sample_rate_hz = 768000; sc.flags = KCTL_SC_CENTER | KCTL_SC_RATE;
    struct kctl_applied_v1 ap; memset(&ap, 0, sizeof ap);
    ap.capture_gen = 14; ap.center_hz = 7074000; ap.sample_rate_hz = 768000; ap.verified = 1;
    struct kctl_status_v1 st; memset(&st, 0, sizeof st);
    st.state = KCTL_STATE_FAILED; st.result = KCTL_E_DEVICE; st.capture_gen = 14;

    printf("{\"frame\":{\"size\":%zu,\"offsets\":{", sizeof h);
    OFF(ksdr_frame_header_v1, magic); OFF(ksdr_frame_header_v1, version); OFF(ksdr_frame_header_v1, header_len);
    OFF(ksdr_frame_header_v1, kind); OFF(ksdr_frame_header_v1, format); OFF(ksdr_frame_header_v1, flags);
    OFF(ksdr_frame_header_v1, capture_gen); OFF(ksdr_frame_header_v1, seq);
    OFF(ksdr_frame_header_v1, first_sample_index); OFF(ksdr_frame_header_v1, freq_origin_hz);
    OFF(ksdr_frame_header_v1, host_time_ns); OFF(ksdr_frame_header_v1, gap_samples);
    OFF(ksdr_frame_header_v1, receiver_idx); OFF(ksdr_frame_header_v1, stream_id);
    OFF(ksdr_frame_header_v1, sample_rate_hz);
    printf("\"sample_count\":%zu},", offsetof(struct ksdr_frame_header_v1, sample_count));
    printf("\"consts\":{\"magic\":%u,\"version\":%u,\"header_len\":%u,"
           "\"kind_samples\":%d,\"kind_eos\":%d,\"fmt_ci16\":%d,\"fmt_cf32\":%d,\"fmt_ri16\":%d,\"fmt_rf32\":%d,"
           "\"f_disc\":%u,\"f_gap\":%u,\"f_ovf\":%u,\"f_newgen\":%u,\"f_mask\":%u,"
           "\"bps\":[%u,%u,%u,%u,%u]},",
           KSDR_FRAME_MAGIC, KSDR_FRAME_VERSION, KSDR_FRAME_HEADER_LEN_V1,
           KSDR_KIND_SAMPLES, KSDR_KIND_END_OF_STREAM, KSDR_FMT_CI16, KSDR_FMT_CF32, KSDR_FMT_RI16, KSDR_FMT_RF32,
           (unsigned)KSDR_F_DISCONTINUITY, (unsigned)KSDR_F_GAP_KNOWN, (unsigned)KSDR_F_OVERFLOW,
           (unsigned)KSDR_F_NEW_GEN, (unsigned)KSDR_F_DEFINED_MASK,
           ksdr_bytes_per_sample(0), ksdr_bytes_per_sample(1), ksdr_bytes_per_sample(2),
           ksdr_bytes_per_sample(3), ksdr_bytes_per_sample(4));
    printf("\"sample\":"); hex(&h, sizeof h); printf("},");

    printf("\"ctl\":{\"sizes\":{\"header\":%zu,\"set_capture\":%zu,\"ack\":%zu,\"applied\":%zu,\"failed\":%zu,\"status\":%zu},",
           sizeof(struct kctl_header_v1), sizeof(struct kctl_set_capture_v1), sizeof(struct kctl_ack_v1),
           sizeof(struct kctl_applied_v1), sizeof(struct kctl_failed_v1), sizeof(struct kctl_status_v1));
    printf("\"offsets\":{");
    OFF(kctl_header_v1, magic); OFF(kctl_header_v1, version); OFF(kctl_header_v1, header_len);
    OFF(kctl_header_v1, msg_type); OFF(kctl_header_v1, reserved0); OFF(kctl_header_v1, payload_len);
    printf("\"cmd_seq\":%zu},", offsetof(struct kctl_header_v1, cmd_seq));
    printf("\"sc_offsets\":{");
    OFF(kctl_set_capture_v1, center_hz); OFF(kctl_set_capture_v1, sample_rate_hz);
    printf("\"flags\":%zu},", offsetof(struct kctl_set_capture_v1, flags));
    printf("\"applied_offsets\":{");
    OFF(kctl_applied_v1, capture_gen); OFF(kctl_applied_v1, center_hz); OFF(kctl_applied_v1, sample_rate_hz);
    printf("\"verified\":%zu},", offsetof(struct kctl_applied_v1, verified));
    printf("\"status_offsets\":{");
    OFF(kctl_status_v1, state); OFF(kctl_status_v1, result);
    printf("\"capture_gen\":%zu},", offsetof(struct kctl_status_v1, capture_gen));
    printf("\"consts\":{\"magic\":%u,\"version\":%u,\"header_len\":%u,\"max_payload\":%u,"
           "\"set_capture\":%d,\"stop\":%d,\"ping\":%d,\"ack\":%d,\"applied\":%d,\"failed\":%d,\"pong\":%d,\"status\":%d,"
           "\"ok\":%d,\"e_unsupported\":%d,\"e_out_of_range\":%d,\"e_device\":%d,\"e_busy\":%d,\"e_bad_message\":%d,\"e_timeout\":%d,"
           "\"st_starting\":%d,\"st_running\":%d,\"st_stopping\":%d,\"st_failed\":%d,"
           "\"sc_center\":%u,\"sc_rate\":%u,\"sc_mask\":%u},",
           KCTL_MAGIC, KCTL_VERSION, KCTL_HEADER_LEN_V1, KCTL_MAX_PAYLOAD,
           KCTL_MSG_SET_CAPTURE, KCTL_MSG_STOP, KCTL_MSG_PING, KCTL_MSG_ACK, KCTL_MSG_APPLIED, KCTL_MSG_FAILED,
           KCTL_MSG_PONG, KCTL_MSG_STATUS,
           KCTL_OK, KCTL_E_UNSUPPORTED, KCTL_E_OUT_OF_RANGE, KCTL_E_DEVICE, KCTL_E_BUSY, KCTL_E_BAD_MESSAGE, KCTL_E_TIMEOUT,
           KCTL_STATE_STARTING, KCTL_STATE_RUNNING, KCTL_STATE_STOPPING, KCTL_STATE_FAILED,
           (unsigned)KCTL_SC_CENTER, (unsigned)KCTL_SC_RATE, (unsigned)KCTL_SC_DEFINED_MASK);
    printf("\"hdr_sample\":"); hex(&ch, sizeof ch);
    printf(",\"sc_sample\":"); hex(&sc, sizeof sc);
    printf(",\"applied_sample\":"); hex(&ap, sizeof ap);
    printf(",\"status_sample\":"); hex(&st, sizeof st);
    printf("}}\n");
    return 0;
}
"""

FLAGS = ["-Wall", "-Wextra", "-Werror", "-pedantic"]


def _compile_and_run(tmp_path: Path, protocol_dir: Path, lang: str) -> dict:
    if lang == "c":
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        src, std = tmp_path / "probe.c", "-std=c11"
    else:
        cc = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        src, std = tmp_path / "probe.cpp", "-std=c++17"
    if cc is None:
        pytest.fail(f"no {lang} compiler found; install build-essential")
    src.write_text(PROBE)
    exe = tmp_path / f"probe_{lang}"
    subprocess.run([cc, std, *FLAGS, f"-I{protocol_dir}", str(src), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


@pytest.fixture(scope="module", params=["c", "cpp"])
def probe(request, tmp_path_factory, protocol_dir) -> dict:
    return _compile_and_run(tmp_path_factory.mktemp(request.param), protocol_dir, request.param)


# ------------------------------------------------------------------ frame

def test_frame_layout(probe):
    f = probe["frame"]
    assert f["size"] == frame.HEADER_LEN_V1 == 80
    assert f["offsets"] == frame.field_offsets()


def test_frame_constants(probe):
    c = probe["frame"]["consts"]
    assert c["magic"] == frame.MAGIC and c["version"] == frame.VERSION and c["header_len"] == frame.HEADER_LEN_V1
    assert (c["kind_samples"], c["kind_eos"]) == (frame.FrameKind.SAMPLES, frame.FrameKind.END_OF_STREAM)
    assert [c["fmt_ci16"], c["fmt_cf32"], c["fmt_ri16"], c["fmt_rf32"]] == [int(x) for x in frame.SampleFormat]
    assert [c["f_disc"], c["f_gap"], c["f_ovf"], c["f_newgen"]] == [int(x) for x in frame.FrameFlags]
    assert c["f_mask"] == frame.FLAGS_DEFINED_MASK
    assert c["bps"] == [0] + [fmt.bytes_per_sample for fmt in frame.SampleFormat]


def _expected_frame_header() -> frame.FrameHeader:
    F = frame.FrameFlags
    return frame.FrameHeader(
        kind=frame.FrameKind.SAMPLES, format=frame.SampleFormat.RI16,
        flags=F.DISCONTINUITY | F.GAP_KNOWN | F.NEW_GEN,
        capture_gen=13, seq=0x0102030405060708, first_sample_index=123456789012,
        freq_origin_hz=-1, host_time_ns=1793035503000000000, gap_samples=4096,
        receiver_idx=2, stream_id=7, sample_rate_hz=64_000_000, sample_count=16384,
    )


def test_frame_bytes_identical_both_directions(probe):
    c_bytes = bytes.fromhex(probe["frame"]["sample"])
    expected = _expected_frame_header()
    assert expected.pack() == c_bytes
    decoded = frame.FrameHeader.unpack(c_bytes)
    assert decoded == expected
    assert decoded.payload_len == 16384 * 2


# ------------------------------------------------------------ worker ctl

def test_ctl_layout(probe):
    c = probe["ctl"]
    assert c["sizes"] == wc.SIZES
    assert c["offsets"] == wc.offsets(wc.HEADER_FIELDS)
    assert c["sc_offsets"] == wc.offsets(wc.SET_CAPTURE_FIELDS)
    assert c["applied_offsets"] == wc.offsets(wc.APPLIED_FIELDS)
    assert c["status_offsets"] == wc.offsets(wc.STATUS_FIELDS)


def test_ctl_constants(probe):
    c = probe["ctl"]["consts"]
    assert (c["magic"], c["version"], c["header_len"], c["max_payload"]) == \
        (wc.MAGIC, wc.VERSION, wc.HEADER_LEN_V1, wc.MAX_PAYLOAD)
    assert [c[k] for k in ("set_capture", "stop", "ping", "ack", "applied", "failed", "pong", "status")] == \
        [int(x) for x in wc.MsgType]
    assert [c[k] for k in ("ok", "e_unsupported", "e_out_of_range", "e_device", "e_busy", "e_bad_message",
                           "e_timeout")] == [int(x) for x in wc.Result]
    assert [c[k] for k in ("st_starting", "st_running", "st_stopping", "st_failed")] == \
        [int(x) for x in wc.WorkerState]
    assert (c["sc_center"], c["sc_rate"], c["sc_mask"]) == \
        (wc.SetCaptureFlags.CENTER, wc.SetCaptureFlags.RATE, wc.SC_DEFINED_MASK)


def test_ctl_bytes_identical(probe):
    c = probe["ctl"]
    hdr = wc.CtlHeader(wc.MsgType.SET_CAPTURE, wc.SIZES["set_capture"], 42)
    assert hdr.pack().hex() == c["hdr_sample"]
    assert wc.CtlHeader.unpack(bytes.fromhex(c["hdr_sample"])) == hdr

    sc = wc.SetCapture(center_hz=7_074_000, sample_rate_hz=768_000)
    assert sc.pack_payload().hex() == c["sc_sample"]
    assert wc.SetCapture.unpack_payload(bytes.fromhex(c["sc_sample"])) == sc

    ap = wc.Applied(capture_gen=14, center_hz=7_074_000, sample_rate_hz=768_000, verified=1)
    assert ap.pack_payload().hex() == c["applied_sample"]
    assert wc.Applied.unpack_payload(bytes.fromhex(c["applied_sample"])) == ap

    st = wc.Status(wc.WorkerState.FAILED, wc.Result.E_DEVICE, 14)
    assert st.pack_payload().hex() == c["status_sample"]
    assert wc.Status.unpack_payload(bytes.fromhex(c["status_sample"])) == st
