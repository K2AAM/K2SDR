"""M1 protocol additions: the Python mirrors match frame_v1.h / worker_ctl_v1.h exactly.

SPECTRUM/AUDIO kinds, U8DB format, the two header extensions, CHANNEL_* messages,
CHANNEL_APPLIED, modes, AGC values and E_NOT_FOUND. Compiled as C11 and C++17 like test_abi.py.
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
    putchar('"'); for (size_t i = 0; i < n; i++) printf("%02x", b[i]); putchar('"');
}

int main(void) {
    struct ksdr_spectrum_ext_v1 se; memset(&se, 0, sizeof se);
    se.db_floor_mdb = -127500; se.db_step_mdb = 500; se.blocks = 14;
    struct ksdr_audio_ext_v1 ae; memset(&ae, 0, sizeof ae);
    ae.channel_gen = 0x0102030405060708ull; ae.mode = KCTL_MODE_CW; ae.agc = KCTL_AGC_SLOW;
    ae.level_cdb = -4321; ae.pass_lo_hz = -250; ae.pass_hi_hz = 250;
    struct kctl_channel_v1 ch; memset(&ch, 0, sizeof ch);
    ch.channel_id = 0xA1B2C3D4u; ch.mode = KCTL_MODE_LSB; ch.agc = KCTL_AGC_OFF; ch.offset_hz = -123456789012ll;
    ch.pass_lo_hz = -2700; ch.pass_hi_hz = -300; ch.bfo_hz = 0 /* ignored: DEFAULT_BFO */; ch.manual_gain_cdb = -1250;
    ch.flags = KCTL_CH_DEFAULT_BFO;
    struct kctl_channel_close_v1 cl; memset(&cl, 0, sizeof cl); cl.channel_id = 99;
    struct kctl_channel_applied_v1 ap; memset(&ap, 0, sizeof ap);
    ap.channel_gen = 77; ap.capture_gen = 5; ap.channel = ch; ap.channel.flags = 0; ap.channel_rate_hz = 12000; ap.open = 1;

    printf("{\"consts\":{\"kind_spectrum\":%d,\"kind_audio\":%d,\"fmt_u8db\":%d,\"bps_u8db\":%u,"
           "\"spec_hlen\":%u,\"audio_hlen\":%u,"
           "\"open\":%d,\"tune\":%d,\"close\":%d,\"applied\":%d,\"e_not_found\":%d,"
           "\"modes\":[%d,%d,%d,%d,%d,%d],\"agcs\":[%d,%d,%d,%d],"
           "\"ch_default_pb\":%u,\"ch_default_bfo\":%u,\"ch_mask\":%u,\"max_channels\":%u},",
           KSDR_KIND_SPECTRUM, KSDR_KIND_AUDIO, KSDR_FMT_U8DB, ksdr_bytes_per_sample(KSDR_FMT_U8DB),
           KSDR_SPECTRUM_HEADER_LEN, KSDR_AUDIO_HEADER_LEN,
           KCTL_MSG_CHANNEL_OPEN, KCTL_MSG_CHANNEL_TUNE, KCTL_MSG_CHANNEL_CLOSE, KCTL_MSG_CHANNEL_APPLIED,
           KCTL_E_NOT_FOUND, KCTL_MODE_AM, KCTL_MODE_USB, KCTL_MODE_LSB, KCTL_MODE_CW, KCTL_MODE_NFM, KCTL_MODE_WFM,
           KCTL_AGC_OFF, KCTL_AGC_FAST, KCTL_AGC_MEDIUM, KCTL_AGC_SLOW,
           (unsigned)KCTL_CH_DEFAULT_PASSBAND, (unsigned)KCTL_CH_DEFAULT_BFO, (unsigned)KCTL_CH_DEFINED_MASK,
           (unsigned)KCTL_MAX_CHANNELS);
    printf("\"sizes\":{\"spectrum_ext\":%zu,\"audio_ext\":%zu,\"channel\":%zu,\"channel_close\":%zu,"
           "\"channel_applied\":%zu},",
           sizeof se, sizeof ae, sizeof ch, sizeof cl, sizeof ap);
    printf("\"audio_ext_offsets\":{"); OFF(ksdr_audio_ext_v1, channel_gen); OFF(ksdr_audio_ext_v1, mode);
    OFF(ksdr_audio_ext_v1, agc); OFF(ksdr_audio_ext_v1, level_cdb); OFF(ksdr_audio_ext_v1, pass_lo_hz);
    printf("\"pass_hi_hz\":%zu},", offsetof(struct ksdr_audio_ext_v1, pass_hi_hz));
    printf("\"channel_offsets\":{"); OFF(kctl_channel_v1, channel_id); OFF(kctl_channel_v1, mode);
    OFF(kctl_channel_v1, agc); OFF(kctl_channel_v1, offset_hz); OFF(kctl_channel_v1, pass_lo_hz);
    OFF(kctl_channel_v1, pass_hi_hz); OFF(kctl_channel_v1, bfo_hz); OFF(kctl_channel_v1, manual_gain_cdb);
    OFF(kctl_channel_v1, flags); printf("\"reserved\":%zu},", offsetof(struct kctl_channel_v1, reserved));
    printf("\"samples\":{\"spectrum_ext\":"); hex(&se, sizeof se);
    printf(",\"audio_ext\":"); hex(&ae, sizeof ae);
    printf(",\"channel\":"); hex(&ch, sizeof ch);
    printf(",\"channel_close\":"); hex(&cl, sizeof cl);
    printf(",\"channel_applied\":"); hex(&ap, sizeof ap);
    printf("}}\n");
    return 0;
}
"""

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", params=["c11", "c++17"])
def probe(request, tmp_path_factory) -> dict:
    cc = shutil.which("cc") if request.param == "c11" else shutil.which("c++")
    assert cc, "a C/C++ compiler is required"
    d = tmp_path_factory.mktemp(request.param.replace("+", "p"))
    src = d / ("probe.c" if request.param == "c11" else "probe.cpp")
    src.write_text(PROBE)
    exe = d / "probe"
    std = "-std=c11" if request.param == "c11" else "-std=c++17"
    subprocess.run([cc, std, "-Wall", "-Wextra", "-Werror", "-pedantic", "-I", str(ROOT / "protocol"), str(src),
                    "-o", str(exe)], check=True, capture_output=True)
    return json.loads(subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout)


def test_constants_match(probe):
    c = probe["consts"]
    assert (c["kind_spectrum"], c["kind_audio"]) == (frame.FrameKind.SPECTRUM, frame.FrameKind.AUDIO)
    assert c["fmt_u8db"] == frame.SampleFormat.U8DB and c["bps_u8db"] == frame.SampleFormat.U8DB.bytes_per_sample
    assert (c["spec_hlen"], c["audio_hlen"]) == (frame.SPECTRUM_HEADER_LEN, frame.AUDIO_HEADER_LEN)
    assert (c["open"], c["tune"], c["close"], c["applied"]) == (
        wc.MsgType.CHANNEL_OPEN, wc.MsgType.CHANNEL_TUNE, wc.MsgType.CHANNEL_CLOSE, wc.MsgType.CHANNEL_APPLIED)
    assert c["e_not_found"] == wc.Result.E_NOT_FOUND
    assert c["modes"] == [m.value for m in wc.Mode] and c["agcs"] == [a.value for a in wc.Agc]
    assert (c["ch_default_pb"], c["ch_default_bfo"], c["ch_mask"]) == (
        wc.ChannelFlags.DEFAULT_PASSBAND, wc.ChannelFlags.DEFAULT_BFO, wc.CH_DEFINED_MASK)
    assert c["max_channels"] == wc.MAX_CHANNELS


def test_sizes_and_offsets_match(probe):
    s = probe["sizes"]
    assert s["spectrum_ext"] == frame.SPECTRUM_HEADER_LEN - frame.HEADER_LEN_V1
    assert s["audio_ext"] == frame.AUDIO_HEADER_LEN - frame.HEADER_LEN_V1
    assert (s["channel"], s["channel_close"], s["channel_applied"]) == (
        wc.SIZES["channel"], wc.SIZES["channel_close"], wc.SIZES["channel_applied"])
    assert probe["audio_ext_offsets"] == frame.ext_offsets(frame.AUDIO_EXT_FIELDS)
    assert probe["channel_offsets"] == wc.offsets(wc.CHANNEL_FIELDS)


def test_packed_samples_are_byte_identical(probe):
    smp = {k: bytes.fromhex(v) for k, v in probe["samples"].items()}
    assert frame.SpectrumExt(-127500, 500, 14).pack() == smp["spectrum_ext"]
    ae = frame.AudioExt(0x0102030405060708, wc.Mode.CW, wc.Agc.SLOW, -4321, -250, 250)
    assert ae.pack() == smp["audio_ext"] and frame.AudioExt.unpack(smp["audio_ext"]) == ae
    ch = wc.Channel(0xA1B2C3D4, wc.Mode.LSB, wc.Agc.OFF, -123456789012, -2700, -300, None, -1250)
    assert ch.pack_payload() == smp["channel"]
    assert wc.Channel.unpack_payload(smp["channel"]) == ch
    assert wc.pack_channel_close(99) == smp["channel_close"]
    ap = wc.ChannelApplied.unpack_payload(smp["channel_applied"])
    assert (ap.channel_gen, ap.capture_gen, ap.channel_rate_hz, ap.open) == (77, 5, 12000, True)
    assert ap.channel.offset_hz == -123456789012 and ap.channel.bfo_hz == 0      # flags cleared: raw value
    assert ap.pack_payload() == smp["channel_applied"]


def test_mirror_rejects_inconsistent_m1_frames():
    base = dict(kind=frame.FrameKind.AUDIO, format=frame.SampleFormat.RI16, flags=frame.FrameFlags(0),
                capture_gen=1, seq=0, first_sample_index=0, freq_origin_hz=7_074_000, host_time_ns=0,
                gap_samples=0, receiver_idx=0, stream_id=1, sample_rate_hz=12000, sample_count=0,
                header_len=frame.AUDIO_HEADER_LEN, ext=frame.AudioExt(1, 2, 3, 0, 300, 2700).pack())
    frame.FrameHeader(**base).validate()
    for bad in ({"format": frame.SampleFormat.CI16}, {"sample_rate_hz": 48000}, {"header_len": 80, "ext": b""}):
        with pytest.raises(frame.FrameError):
            frame.FrameHeader(**{**base, **bad}).pack()
    with pytest.raises(frame.FrameError):                                  # passband lo >= hi
        frame.FrameHeader.unpack(frame.FrameHeader(**{**base, "ext": frame.AudioExt(1, 2, 3, 0, 9, 9).pack()}
                                                   ).pack()[:80]).with_ext(frame.AudioExt(1, 2, 3, 0, 9, 9).pack())
    with pytest.raises(frame.FrameError):                                  # U8DB outside SPECTRUM
        frame.FrameHeader(**{**base, "kind": frame.FrameKind.SAMPLES, "format": frame.SampleFormat.U8DB,
                             "header_len": 80, "ext": b"", "sample_rate_hz": 48000}).validate()


def test_channel_payload_needs_both_passband_edges():
    with pytest.raises(wc.CtlError):
        wc.Channel(1, wc.Mode.USB, wc.Agc.FAST, 0, pass_lo_hz=300).pack_payload()
