#!/usr/bin/env python3
"""Fetch the pinned liquid-dsp source and verify it before anything builds from it.

    fetch_liquid.py --dest DIR                 # git fetch of the pinned commit (needs network)
    fetch_liquid.py --dest DIR --from SRC      # offline: copy an existing tree, verify it the same way
    fetch_liquid.py --manifest TREE            # print the content manifest hash of a tree

Two checks, both must pass: the fetched commit id equals PIN_COMMIT (git fetch path), and a
SHA-256 over the source tree's content (every file's relative path, type/exec bit and SHA-256,
sorted; .git excluded) equals PIN_MANIFEST_SHA256. The manifest does not depend on how an
archive was compressed. DIR is replaced only after verification succeeds; it is project-local
(build/third_party/...) and nothing outside it is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PIN_NAME = "liquid-dsp"
PIN_VERSION = "1.8.3"
PIN_REPO = "https://github.com/jgaeddert/liquid-dsp.git"
PIN_COMMIT = "10041f70cebbe3b97887e75bb41e48b73dda1b23"        # tag v1.8.3
PIN_MANIFEST_SHA256 = "0ce25baae2ab399a3b00f6cb12db5e4e8ac508ed0e5738435aa65e1e24df87f4"
MARKER = ".k2-verified"


def manifest(tree: Path) -> str:
    h = hashlib.sha256()
    entries = []
    for root, dirs, files in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if not (Path(root) == tree and d == ".git"))
        for name in files:
            p = Path(root) / name
            rel = p.relative_to(tree).as_posix()
            if rel == MARKER:
                continue
            if p.is_symlink():
                entries.append((rel, "l", hashlib.sha256(os.readlink(p).encode()).hexdigest()))
            else:
                kind = "x" if os.access(p, os.X_OK) else "f"
                entries.append((rel, kind, hashlib.sha256(p.read_bytes()).hexdigest()))
        for d in list(dirs):
            if (Path(root) / d).is_symlink():          # never follow symlinked directories
                dirs.remove(d)
                rel = (Path(root) / d).relative_to(tree).as_posix()
                entries.append((rel, "l", hashlib.sha256(os.readlink(Path(root) / d).encode()).hexdigest()))
    for rel, kind, digest in sorted(entries):
        h.update(f"{rel}\0{kind}\0{digest}\n".encode())
    return h.hexdigest()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch(dest: Path, src: Path | None) -> None:
    if (dest / MARKER).is_file() and (dest / MARKER).read_text().strip() == f"{PIN_COMMIT} {PIN_MANIFEST_SHA256}":
        if manifest(dest) == PIN_MANIFEST_SHA256:            # re-checked every time: cheap, and edits show up
            print(f"{PIN_NAME} {PIN_VERSION}: verified source at {dest}")
            return
        print(f"{PIN_NAME}: {dest} no longer matches the pinned manifest; fetching again")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".liquid-fetch-", dir=dest.parent) as tmp:
        work = Path(tmp) / "src"
        if src is None:
            work.mkdir()
            git("init", "-q", cwd=work)
            git("fetch", "-q", "--depth", "1", PIN_REPO, PIN_COMMIT, cwd=work)
            git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=work)
            got = git("rev-parse", "HEAD", cwd=work)
            if got != PIN_COMMIT:
                raise SystemExit(f"commit mismatch: fetched {got}, pinned {PIN_COMMIT}")
            shutil.rmtree(work / ".git")
        else:
            shutil.copytree(src, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        got = manifest(work)
        if got != PIN_MANIFEST_SHA256:
            raise SystemExit(f"{PIN_NAME} source manifest mismatch:\n  got    {got}\n  pinned {PIN_MANIFEST_SHA256}")
        (work / MARKER).write_text(f"{PIN_COMMIT} {PIN_MANIFEST_SHA256}\n")
        if dest.exists():
            shutil.rmtree(dest)
        work.rename(dest)
    print(f"{PIN_NAME} {PIN_VERSION} ({PIN_COMMIT[:12]}): source verified, manifest {PIN_MANIFEST_SHA256[:16]}…")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path)
    ap.add_argument("--from", dest="src", type=Path)
    ap.add_argument("--manifest", type=Path)
    a = ap.parse_args()
    if a.manifest:
        print(manifest(a.manifest.resolve()))
        return 0
    if not a.dest:
        ap.error("--dest is required")
    fetch(a.dest.resolve(), a.src.resolve() if a.src else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
