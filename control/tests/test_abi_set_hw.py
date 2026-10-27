"""SET_HW / HW_APPLIED (M1 step 4a): the Python mirror matches worker_ctl_v1.h, as C11 and C++17."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from k2websdr.protocol import worker_ctl as wc

ROOT = Path(__file__).resolve().parents[2]
PROBE = r"""
#include <stdio.h>
#include <string.h>
#include "worker_ctl_v1.h"
static void hex(const void *p, size_t n) {
    const unsigned char *b = (const unsigned char *)p;
    putchar('"'); for (size_t i = 0; i < n; i++) printf("%02x", b[i]); putchar('"');
}
int main(void) {
    struct kctl_set_hw_v1 s; memset(&s, 0, sizeof s);
    s.flags = KCTL_HW_AGC | KCTL_HW_ATT; s.hf_agc = 0; s.hf_att = 7;
    struct kctl_hw_applied_v1 a; memset(&a, 0, sizeof a);
    a.capture_gen = 0x0102030405060708ull; a.hf_agc = 1; a.hf_agc_threshold = 1; a.hf_lna = 0; a.hf_att = 8;
    printf("{\"set_hw\":%u,\"hw_applied\":%u,\"applied_msg\":%u,\"sizes\":[%zu,%zu],"
           "\"flags\":[%u,%u,%u,%u,%u],\"att_max\":%u,\"offs\":[%zu,%zu,%zu,%zu,%zu,%zu,%zu,%zu,%zu],",
           (unsigned)KCTL_MSG_SET_HW, (unsigned)KCTL_MSG_HW_APPLIED, (unsigned)KCTL_MSG_HW_APPLIED, sizeof s, sizeof a,
           (unsigned)KCTL_HW_AGC, (unsigned)KCTL_HW_AGC_THRESHOLD, (unsigned)KCTL_HW_LNA, (unsigned)KCTL_HW_ATT,
           (unsigned)KCTL_HW_DEFINED_MASK, (unsigned)KCTL_HW_ATT_MAX,
           offsetof(struct kctl_set_hw_v1, hf_agc), offsetof(struct kctl_set_hw_v1, hf_agc_threshold),
           offsetof(struct kctl_set_hw_v1, hf_lna), offsetof(struct kctl_set_hw_v1, hf_att),
           offsetof(struct kctl_set_hw_v1, reserved),
           offsetof(struct kctl_hw_applied_v1, hf_agc), offsetof(struct kctl_hw_applied_v1, hf_lna),
           offsetof(struct kctl_hw_applied_v1, hf_att), offsetof(struct kctl_hw_applied_v1, verified));
    printf("\"s\":"); hex(&s, sizeof s); printf(",\"a\":"); hex(&a, sizeof a); printf("}\n");
    return 0;
}
"""


@pytest.fixture(scope="module", params=["c11", "c++17"])
def probe(request, tmp_path_factory) -> dict:
    cc = shutil.which("cc") if request.param == "c11" else shutil.which("c++")
    assert cc, "a C/C++ compiler is required"
    d = tmp_path_factory.mktemp(request.param.replace("+", "p"))
    src = d / ("probe.c" if request.param == "c11" else "probe.cpp")
    src.write_text(PROBE)
    std = "-std=c11" if request.param == "c11" else "-std=c++17"
    subprocess.run([cc, std, "-Wall", "-Wextra", "-Werror", "-pedantic", "-I", str(ROOT / "protocol"), str(src),
                    "-o", str(d / "probe")], check=True, capture_output=True)
    return json.loads(subprocess.run([str(d / "probe")], check=True, capture_output=True, text=True).stdout)


def test_constants_sizes_offsets(probe):
    assert (probe["set_hw"], probe["hw_applied"]) == (wc.MsgType.SET_HW, wc.MsgType.HW_APPLIED)
    assert probe["sizes"] == [wc.SIZES["set_hw"], wc.SIZES["hw_applied"]]
    assert probe["flags"] == [wc.HwFlags.AGC, wc.HwFlags.AGC_THRESHOLD, wc.HwFlags.LNA, wc.HwFlags.ATT,
                              wc.HW_DEFINED_MASK]
    assert probe["att_max"] == wc.HW_ATT_MAX
    s, a = wc.offsets(wc.SET_HW_FIELDS), wc.offsets(wc.HW_APPLIED_FIELDS)
    assert probe["offs"] == [s["hf_agc"], s["hf_agc_threshold"], s["hf_lna"], s["hf_att"], s["reserved"],
                             a["hf_agc"], a["hf_lna"], a["hf_att"], a["verified"]]
    assert wc.PAYLOAD_LEN[wc.MsgType.SET_HW] == wc.SIZES["set_hw"]


def test_packed_samples_are_byte_identical(probe):
    assert wc.SetHw(hf_agc=False, hf_att=7).pack_payload() == bytes.fromhex(probe["s"])
    ap = wc.HwApplied.unpack_payload(bytes.fromhex(probe["a"]))
    assert ap == wc.HwApplied(0x0102030405060708, True, True, False, 8, 0)
    assert ap.pack_payload() == bytes.fromhex(probe["a"])


def test_mirror_refuses_bad_set_hw():
    for bad in (wc.SetHw(), wc.SetHw(hf_att=9), wc.SetHw(hf_att=-1)):
        with pytest.raises(wc.CtlError):
            bad.pack_payload()
