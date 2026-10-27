#!/usr/bin/env python3
"""Prove the DSP build uses the pinned liquid-dsp and contains no FFTW.

    check_dsp_deps.py BUILD_DIR LIQUID_PREFIX [--expect-fftw-installed] [--expect-debian-liquid]

Checks, all of which must pass (nonzero exit otherwise; JSON report on stdout):

liquid-dsp itself
  * k2-build-info.json names the pinned version, commit and source manifest
    (tools/fetch_liquid.py) and records FIND_FFTW=OFF and LIQUID_FFTOVERRIDE.
  * Its CMake cache has FIND_FFTW=OFF and never located an FFTW library or header.
  * libliquid.a: no undefined fftw* symbol; the only fftw-named symbol is liquid's own
    liquid_fftwf_cleanup_wrapper (defined, body compiled out without FFTW).

each binary (ksdr-worker, ksdr-dsp-check)
  * Dynamic NEEDED entries are only the C/C++ runtime (no fftw, no shared liquid).
  * Symbol table: no fftw symbol apart from the wrapper above. ldd/NEEDED cannot see a
    statically linked FFTW; its fftwf_* functions would show up here.
  * Linker map (-Wl,-Map): no input file whose path mentions fftw, exactly one libliquid
    input, and it is this prefix's libliquid.a (not Debian's /usr/lib/.../libliquid*).
  * Link command lines in build.ninja mention no fftw.
  * --version reports the pinned liquid-dsp at runtime.

--expect-fftw-installed / --expect-debian-liquid make the run fail unless FFTW (and Debian's
own liquid-dsp) are actually installed, so CI proves exclusion with them present.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_liquid as pin  # noqa: E402

ALLOWED_FFTW_NAMED = {"liquid_fftwf_cleanup_wrapper"}
ALLOWED_NEEDED = re.compile(r"^(libc\.so\.6|libm\.so\.6|libstdc\+\+\.so\.6|libgcc_s\.so\.1|libpthread\.so\.0|"
                            r"ld-linux-[\w-]+\.so\.\d)$")
BINARIES = ("ksdr-worker", "ksdr-dsp-check")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def nm_symbols(path: Path) -> list[tuple[str, str]]:
    """(type, name) for every symbol, archives included."""
    out = []
    for line in run("nm", "-A", "--no-sort", str(path)).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[-2], parts[-1]))
    return out


def fftw_named(symbols) -> list[str]:
    return sorted({n for _, n in symbols if "fftw" in n.lower() and n not in ALLOWED_FFTW_NAMED})


def map_inputs(map_path: Path) -> list[str]:
    """Every file the linker loaded or took an archive member from."""
    inputs = set()
    for line in map_path.read_text(errors="replace").splitlines():
        m = re.match(r"^LOAD (\S+)", line)
        if m:
            inputs.add(m.group(1))
            continue
        m = re.match(r"^(/\S+?\.a)\(", line)              # "Archive member included ..." section
        if m:
            inputs.add(m.group(1))
    return sorted(inputs)


def check(build: Path, prefix: Path, expect_fftw: bool, expect_debian_liquid: bool) -> dict:
    fails: list[str] = []
    rep: dict = {"build": str(build), "liquid_prefix": str(prefix)}
    lib = (prefix / "lib/libliquid.a").resolve()

    info = json.loads((prefix / "k2-build-info.json").read_text())
    rep["liquid"] = {k: info.get(k) for k in ("version", "commit", "source_manifest_sha256", "simd_detected_on_build_host")}
    if (info.get("version"), info.get("commit"), info.get("source_manifest_sha256")) != \
            (pin.PIN_VERSION, pin.PIN_COMMIT, pin.PIN_MANIFEST_SHA256):
        fails.append("k2-build-info.json does not match the pin in tools/fetch_liquid.py")
    opts = info.get("cmake_options", [])
    if "-DFIND_FFTW=OFF" not in opts or not any("LIQUID_FFTOVERRIDE" in o for o in opts):
        fails.append("liquid-dsp was not configured with FIND_FFTW=OFF and LIQUID_FFTOVERRIDE")

    cache = build.parent / "third_party/liquid-dsp-build/CMakeCache.txt"
    if cache.is_file():
        c = cache.read_text()
        if "FIND_FFTW:BOOL=OFF" not in c.splitlines():
            fails.append("liquid-dsp CMake cache: FIND_FFTW is not OFF")
        found = re.findall(r"^fftw3f_(?:LIBRARY|INCLUDE_DIR):\w+=(/\S*)", c, re.M)
        if found:
            fails.append(f"liquid-dsp CMake cache located FFTW: {found}")
    else:
        fails.append(f"missing {cache}")

    syms = nm_symbols(lib)
    undefined_fftw = sorted({n for t, n in syms if t == "U" and "fftw" in n.lower()})
    rep["libliquid_fftw_symbols"] = sorted({n for _, n in syms if "fftw" in n.lower()})
    if undefined_fftw or fftw_named(syms):
        fails.append(f"libliquid.a references FFTW: {undefined_fftw or fftw_named(syms)}")

    rep["binaries"] = {}
    ninja = (build / "build.ninja").read_text() if (build / "build.ninja").is_file() else ""
    for name in BINARIES:
        b = build / name
        r: dict = {}
        if not b.is_file():
            fails.append(f"{name}: not built at {b}")
            continue
        needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", run("readelf", "-d", str(b)))
        r["needed"] = needed
        bad = [n for n in needed if not ALLOWED_NEEDED.match(n)]
        if bad:
            fails.append(f"{name}: unexpected shared libraries {bad}")
        named = fftw_named(nm_symbols(b))
        if named:
            fails.append(f"{name}: FFTW symbols linked in: {named[:10]}")
        mp = build / f"{name}.map"
        inputs = map_inputs(mp) if mp.is_file() else []
        if not inputs:
            fails.append(f"{name}: no linker map at {mp}")
        if any("fftw" in i.lower() for i in inputs):
            fails.append(f"{name}: linker inputs include FFTW: {[i for i in inputs if 'fftw' in i.lower()]}")
        liquids = sorted({str(Path(i).resolve()) for i in inputs if "libliquid" in Path(i).name})
        r["liquid_inputs"] = liquids
        if liquids != [str(lib)]:
            fails.append(f"{name}: liquid inputs {liquids} are not exactly {lib}")
        link_lines = [ln for ln in ninja.splitlines() if ln.strip().startswith("LINK_") or "-o " + name in ln]
        if any("fftw" in ln.lower() for ln in link_lines):
            fails.append(f"{name}: link command mentions fftw")
        flag = "--version" if name == "ksdr-worker" else "version"
        out = run(str(b), flag).strip()
        r["version_output"] = out
        if pin.PIN_VERSION not in out:
            fails.append(f"{name}: runtime liquid-dsp version is not {pin.PIN_VERSION}: {out}")
        rep["binaries"][name] = r

    fftw_present = [str(p) for p in (*Path("/usr/lib").rglob("libfftw3f.so*"), *Path("/usr/include").glob("fftw3.h"))]
    rep["fftw_on_system"] = fftw_present
    if expect_fftw and not fftw_present:
        fails.append("--expect-fftw-installed: FFTW is not installed, so exclusion was not proven against it")
    debian_liquid = [str(p) for p in (*Path("/usr/include/liquid").glob("liquid.h"),
                                      *Path("/usr/lib").rglob("libliquid.so*"))]
    rep["debian_liquid_on_system"] = debian_liquid
    if expect_debian_liquid and not debian_liquid:
        fails.append("--expect-debian-liquid: Debian's liquid-dsp is not installed")

    rep["ok"] = not fails
    rep["failures"] = fails
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    ap.add_argument("prefix", type=Path)
    ap.add_argument("--expect-fftw-installed", action="store_true")
    ap.add_argument("--expect-debian-liquid", action="store_true")
    a = ap.parse_args()
    rep = check(a.build.resolve(), a.prefix.resolve(), a.expect_fftw_installed, a.expect_debian_liquid)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
