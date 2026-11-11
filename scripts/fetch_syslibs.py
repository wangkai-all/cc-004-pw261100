#!/usr/bin/env python3
"""Download Debian bookworm .deb packages (with dependency closure) and extract
them into a local directory, for systems without root. Used to provide the
shared libraries Playwright's chromium needs (libnss3, libX11, ...).

Usage: python3 scripts/fetch_syslibs.py <dest_dir>
Sets nothing globally; run binaries with LD_LIBRARY_PATH=<dest_dir>/usr/lib/x86_64-linux-gnu
"""
import lzma
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MIRROR = "http://deb.debian.org/debian"
INDEX = f"{MIRROR}/dists/bookworm/main/binary-amd64/Packages.xz"

SEED_PACKAGES = [
    "libatomic1",
    "libasound2", "libatk-bridge2.0-0", "libatk1.0-0", "libatspi2.0-0",
    "libcairo2", "libcups2", "libdbus-1-3", "libexpat1", "libgbm1",
    "libglib2.0-0", "libnspr4", "libnss3", "libpango-1.0-0",
    "libx11-6", "libxcb1", "libxcomposite1", "libxdamage1", "libxext6",
    "libxfixes3", "libxkbcommon0", "libxrandr2",
    # 字体：没有字体和 fontconfig 配置时渲染进程会崩溃（fill/inner_text 失效）
    "fontconfig-config", "libfontconfig1", "fonts-liberation2",
]

# Packages already provided by the base system; don't fetch their deps either.
SKIP = {"libc6", "libstdc++6", "libgcc-s1", "gcc-12-base", "zlib1g", "wget",
        "xdg-utils", "ca-certificates", "fonts-liberation", "dpkg"}


def download(url: str, dest: Path, tries: int = 5):
    for attempt in range(1, tries + 1):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except Exception as e:
            if attempt == tries:
                raise
            print(f"  retry {attempt}/{tries} after: {e}", flush=True)
            time.sleep(2 * attempt)


def fetch_index(cache: Path) -> dict:
    xz = cache / "Packages.xz"
    if not xz.exists():
        print(f"fetching {INDEX}", flush=True)
        download(INDEX, xz)
    pkgs = {}
    name = filename = depends = None
    def commit():
        if name and filename:
            pkgs[name] = {"filename": filename, "depends": depends or ""}
    with lzma.open(xz, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line == "\n":
                commit()
                name = filename = depends = None
            elif line.startswith("Package: "):
                name = line[9:].strip()
            elif line.startswith("Filename: "):
                filename = line[10:].strip()
            elif line.startswith("Depends: "):
                depends = line[9:].strip()
    commit()
    return pkgs


def parse_dep_names(depends: str):
    for part in depends.split(","):
        # take first alternative, strip version constraint and arch qualifier
        first = part.split("|")[0].strip()
        first = re.sub(r"\s*\(.*\)", "", first)
        first = re.sub(r":any$", "", first).strip()
        if first:
            yield first


def main():
    dest = Path(sys.argv[1]).resolve()
    cache = dest / "_debs"
    root = dest / "root"
    cache.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    pkgs = fetch_index(cache)
    todo = [p for p in SEED_PACKAGES if p not in SKIP]
    done = set(SKIP)
    queue = list(todo)
    closure = []
    while queue:
        pkg = queue.pop(0)
        if pkg in done:
            continue
        done.add(pkg)
        info = pkgs.get(pkg)
        if info is None:
            print(f"  skip (virtual/missing): {pkg}")
            continue
        closure.append(pkg)
        for dep in parse_dep_names(info["depends"]):
            if dep not in done:
                queue.append(dep)

    print(f"closure: {len(closure)} packages")
    for pkg in closure:
        info = pkgs[pkg]
        url = f"{MIRROR}/{info['filename']}"
        deb = cache / Path(info["filename"]).name
        if not deb.exists():
            print(f"  download {pkg}", flush=True)
            download(url, deb)
        subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)

    libdir = root / "usr/lib/x86_64-linux-gnu"
    print(f"extracted to {root}")
    print(f"run with: LD_LIBRARY_PATH={libdir}")


if __name__ == "__main__":
    main()
