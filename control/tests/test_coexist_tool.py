"""tools/coexist_check.py: parsing and comparison logic (the trial itself runs on the Optiplex)."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("coexist_check", ROOT / "tools/coexist_check.py")
cc = importlib.util.module_from_spec(spec)
sys.modules["coexist_check"] = cc
spec.loader.exec_module(cc)

PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F9B 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1
   1: 00000000:1F99 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1
   2: 0100007F:1F9B 0100007F:D2A4 01 00000000:00000000 00:00000000 00000000  1001        0 3 1
"""
PROC_TCP6 = """  sl  local_address                         remote_address                        st tx_queue
   0: 00000000000000000000000001000000:1F9B 00000000000000000000000000000000:0000 0A 00000000:00000000
   1: 0000000000000000FFFF00000100007F:0050 00000000000000000000000000000000:0000 0A 00000000:00000000
"""


def test_proc_net_tcp_listen_sockets_only():
    assert cc.parse_proc_net_tcp(PROC_TCP, v6=False) == [("127.0.0.1", 8091), ("0.0.0.0", 8089)]


def test_proc_net_tcp6_including_mapped_ipv4():
    assert cc.parse_proc_net_tcp(PROC_TCP6, v6=True) == [("::1", 8091), ("127.0.0.1", 80)]


def test_usb_devices_from_fake_sysfs(tmp_path):
    def dev(name, vid, pid, bus, num, product=None):
        d = tmp_path / name
        d.mkdir()
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{num}\n")
        if product:
            (d / "product").write_text(product + "\n")
    dev("1-1", "03eb", "800c", 1, 2, "AIRSPY HF+")
    dev("1-2", "8087", "0033", 1, 3)
    dev("2-1", "1df7", "3060", 2, 5, "RSPdx")
    (tmp_path / "1-1:1.0").mkdir()                 # interface dirs have no ids: skipped
    devs = {d["id"]: d for d in cc.usb_devices(tmp_path)}
    assert devs["03eb:800c"]["sdr"] == "Airspy HF+ Discovery" and devs["03eb:800c"]["node"] == "/dev/bus/usb/001/002"
    assert devs["1df7:3060"]["sdr"] == "SDRplay (any model)"
    assert devs["8087:0033"]["sdr"] is None


def _snap():
    return {"owrx": {"active_state": "active", "active_since": "Mon 2026-10-26 12:29:25 EDT", "main_pid": "497210"},
            "owrx_processes": [{"comm": "openwebrx", "pid": 497210, "start": "Mon Oct 26 12:29:25 2026"},
                               {"comm": "soapy_connector", "pid": 497222, "start": "Mon Oct 26 12:29:26 2026"}],
            "owrx_http": 200,
            "usb": [{"id": "03eb:800c", "sdr": "Airspy HF+ Discovery"}]}


def test_compare_identical():
    assert cc.compare_owrx(_snap(), _snap()) == []


def test_compare_detects_every_disturbance():
    base = _snap()
    cases = [
        ("owrx", "main_pid", "999"),
        ("owrx", "active_since", "Mon 2026-10-26 15:00:00 EDT"),
        ("owrx", "active_state", "activating"),
    ]
    for table, key, value in cases:
        after = copy.deepcopy(base)
        after[table][key] = value
        assert cc.compare_owrx(base, after), key
    after = copy.deepcopy(base)
    after["owrx_processes"][1]["pid"] = 500000           # soapy_connector restarted
    assert any("processes changed" in p for p in cc.compare_owrx(base, after))
    after = copy.deepcopy(base)
    after["owrx_http"] = None
    assert any("HTTP" in p for p in cc.compare_owrx(base, after))
    after = copy.deepcopy(base)
    after["usb"] = []
    assert any("SDR USB" in p for p in cc.compare_owrx(base, after))
