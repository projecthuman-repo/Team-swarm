#!/usr/bin/env python3
"""Wheel-footprint report (Lean & Precise L0.1).

Resolves every requirements*.txt set for the deployment target
(Python 3.12, x86_64-manylinux_2_28) with `uv pip compile`, then sums the
compressed size of the wheel each pinned package would install, taken from
the PyPI JSON API. Prints a markdown table: per-set package count, total
compressed MB, and the heaviest packages.

Sizes are compressed wheels (what the image build downloads), not the
unpacked install size. A package with no compatible wheel falls back to its
sdist size and is marked with `*`.

    python3 scripts/footprint_wheels.py                    # all sets
    python3 scripts/footprint_wheels.py requirements.txt   # one set
    python3 scripts/footprint_wheels.py --json             # machine-readable
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PYTHON_VERSION = "3.12"
PLATFORM = "x86_64-manylinux_2_28"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOP_N = 5
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def resolve(req_file: str) -> list[tuple[str, str]]:
    """`uv pip compile` one requirements file for the target platform."""
    proc = subprocess.run(
        [
            "uv", "pip", "compile", "--quiet", "--no-header", "--no-annotate",
            "--python-version", PYTHON_VERSION,
            "--python-platform", PLATFORM,
            req_file,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"uv pip compile {req_file} failed:\n{proc.stderr[-1500:]}")
    pins = []
    for line in proc.stdout.splitlines():
        m = _PIN_RE.match(line.strip())
        if m:
            pins.append((m.group(1).lower(), m.group(2)))
    return pins


def wheel_score(filename: str) -> int:
    """Higher = closer to what uv installs on cp312/manylinux x86_64; 0 = incompatible."""
    if not filename.endswith(".whl"):
        return 0
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return 0
    py, abi, plat = parts[-3], parts[-2], parts[-1]
    plat_ok = plat == "any" or (
        "x86_64" in plat and ("manylinux" in plat or plat.startswith("linux"))
    )
    if not plat_ok:
        return 0
    pys = py.split(".")
    if "cp312" in pys and abi in ("cp312", "abi3", "none"):
        return 4 if plat != "any" else 3
    if abi == "abi3" and any(re.fullmatch(r"cp3(\d+)", p) and int(p[3:]) <= 12 for p in pys):
        return 3
    if any(p.startswith("py3") or p == "py2.py3" for p in pys) and abi == "none":
        return 2
    return 0


def package_size(name: str, version: str) -> tuple[int, bool]:
    """(compressed bytes, is_sdist_fallback) for one pinned package."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:  # nosec B310 - fixed https://pypi.org host
        files = json.load(resp).get("urls", [])
    wheels = sorted(
        ((wheel_score(f["filename"]), f["size"]) for f in files),
        reverse=True,
    )
    if wheels and wheels[0][0] > 0:
        return wheels[0][1], False
    sdists = [f["size"] for f in files if f.get("packagetype") == "sdist"]
    return (sdists[0] if sdists else 0), True


def report(req_files: list[str]) -> dict:
    sets = {os.path.basename(f): resolve(f) for f in req_files}
    unique = sorted({pin for pins in sets.values() for pin in pins})
    sizes: dict[tuple[str, str], tuple[int, bool]] = {}
    failed: list[str] = []

    def fetch(pin: tuple[str, str]) -> None:
        try:
            sizes[pin] = package_size(*pin)
        except Exception as err:  # noqa: BLE001 - report, never crash the table
            failed.append(f"{pin[0]}=={pin[1]} ({err.__class__.__name__})")
            sizes[pin] = (0, False)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(fetch, unique))

    out = {"python": PYTHON_VERSION, "platform": PLATFORM, "sets": {}, "failed": failed}
    for name, pins in sets.items():
        rows = sorted(((sizes[p][0], p[0], p[1], sizes[p][1]) for p in pins), reverse=True)
        out["sets"][name] = {
            "packages": len(pins),
            "total_bytes": sum(r[0] for r in rows),
            "top": [
                {"name": n, "version": v, "bytes": b, "sdist": s}
                for b, n, v, s in rows[:TOP_N]
            ],
        }
    return out


def to_markdown(data: dict) -> str:
    mb = lambda b: f"{b / 1e6:,.1f}"  # noqa: E731
    lines = [
        f"## Wheel footprint — Python {data['python']} / {data['platform']}",
        "",
        "Compressed wheel bytes per resolved set (PyPI JSON sizes; `*` = sdist fallback).",
        "",
        f"| set | packages | compressed MB | top {TOP_N} packages (MB) |",
        "|---|---:|---:|---|",
    ]
    for name, s in sorted(data["sets"].items(), key=lambda kv: -kv[1]["total_bytes"]):
        top = ", ".join(
            f"{t['name']}{'*' if t['sdist'] else ''} {mb(t['bytes'])}" for t in s["top"]
        )
        lines.append(f"| `{name}` | {s['packages']} | {mb(s['total_bytes'])} | {top} |")
    if data["failed"]:
        lines += ["", f"Size lookup failed for {len(data['failed'])} package(s): "
                  + ", ".join(data["failed"])]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("files", nargs="*", help="requirements files (default: all sets)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = p.parse_args()
    files = args.files or sorted(glob.glob(os.path.join(ROOT, "requirements*.txt")))
    data = report(files)
    print(json.dumps(data, indent=2) if args.json else to_markdown(data))
    # A table with missing sizes understates the footprint: never report
    # it as a success (§1 "never mark green what did not run").
    return 1 if data["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
