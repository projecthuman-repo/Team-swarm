#!/usr/bin/env python3
"""License gate — three zones (Hard Rule 9, runbook §10/§12; L3.1).

Zones (conventions/licensing.md):
  C (default)  image contents / shipped code — permissive allowlist,
               FAIL-CLOSED: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC,
               0BSD (+ equivalents below); anything else blocks.
  B            everything linked anywhere in the repo — fails only on the
               AGPL / SSPL / BUSL (Business Source) / Commons Clause denylist.
  A            network services — prints the services table, exits 0.

    license_gate.py [--zone C] [--paths agent ingest ... uv.lock]
    license_gate.py --zone B [--paths dir:.]
    license_gate.py --zone A

Each path is scanned by syft (`dir:` for directories, `file:` for files),
one scan per path, and the packages merged. A path that does not exist, or
a scan that fails, fails the gate (exit 2) — nothing passes unscanned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LICENSING_MD = os.path.join(ROOT, "conventions", "licensing.md")

ALLOWED = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "MPL-2.0",
    "ISC",
    "0BSD",
    # common compound/equivalent SPDX ids that are still permissive
    "PostgreSQL",
    "Python-2.0",
    "PSF-2.0",
    "Unlicense",
    "BlueOak-1.0.0",
    "CC0-1.0",
}
# Zone C: substring denylist checked before the allowlist (kept from v4.0).
DENIED = ("AGPL", "SSPL", "BUSL", "BSL", "Commons Clause", "Elastic-2.0")
# Zone B: matched per atomic SPDX id. "BSL" alone is Business Source, but
# BSL-1.0 is the (permissive) Boost license, so it is excluded explicitly.
DENIED_B = re.compile(r"AGPL|SSPL|BUSL|^BSL(?!-1\.0)|Commons[ -]Clause", re.I)

DEFAULT_PATHS = {
    "C": [
        "agent",
        "ingest",
        "relay",
        "skills",
        "scripts",
        "pyproject.toml",
        "uv.lock",
        "relay/package-lock.json",
    ],  # fmt: skip
    "B": ["dir:."],
}


def normalize(expr: str) -> list[str]:
    """Split an SPDX expression into atomic ids (handles AND/OR/parens)."""
    for tok in ("(", ")", " AND ", " OR ", " and ", " or "):
        expr = expr.replace(tok, "|")
    return [part.strip() for part in expr.split("|") if part.strip()]


def _license(p: dict) -> str:
    return p.get("licenseConcluded") or p.get("licenseDeclared") or "NOASSERTION"


def check(sbom: dict, zone: str = "C") -> list[tuple[str, str]]:
    bad = []
    for p in sbom.get("packages", []):
        lic = _license(p)
        if lic == "NOASSERTION":
            continue  # syft SPDX root doc entries; deps carry real ids
        if zone == "B":
            if any(DENIED_B.search(part) for part in normalize(lic)):
                bad.append((p.get("name", "?"), lic))
            continue
        if any(d.lower() in lic.lower() for d in DENIED):
            bad.append((p.get("name", "?"), lic))
            continue
        if not all(part in ALLOWED for part in normalize(lic)):
            bad.append((p.get("name", "?"), lic))
    return bad


def services_table(path: str = LICENSING_MD) -> list[dict]:
    """Rows of the '## Services (Zone A)' table in conventions/licensing.md."""
    rows, header, inside = [], None, False
    with open(path) as f:
        for line in f:
            if line.startswith("## "):
                inside = line.strip() == "## Services (Zone A)"
                continue
            if not inside or not line.startswith("|"):
                continue
            cells = [c.replace("`", "").strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells
            elif not set(line.strip()) <= set("|-: "):
                rows.append(dict(zip(header, cells, strict=False)))
    return rows


def source_for(path: str) -> str:
    if ":" in path:
        return path  # already a syft source (dir:., file:x, registry:...)
    full = os.path.join(ROOT, path)
    if os.path.isdir(full):
        return f"dir:{full}"
    if os.path.isfile(full):
        return f"file:{full}"
    raise FileNotFoundError(path)


def scan(paths: list[str]) -> dict:
    """One syft scan per path, packages merged and de-duplicated."""
    merged, seen = [], set()
    for path in paths:
        out = subprocess.check_output(
            ["syft", "packages", "-o", "spdx-json", source_for(path)], cwd=ROOT
        )
        for p in json.loads(out).get("packages", []):
            key = (p.get("name"), p.get("versionInfo"), _license(p))
            if key not in seen:
                seen.add(key)
                merged.append(p)
    return {"packages": merged}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--zone", choices=("A", "B", "C"), default="C")
    ap.add_argument("--paths", nargs="+", help="override the zone's default scan paths")
    args = ap.parse_args(argv)

    if args.zone == "A":
        print("Zone A — network services (any OSI license; isolated, unmodified):")
        for row in services_table():
            print(f"  {row['service']:<22} {row['license']:<18} {row['image']}")
        return 0

    paths = args.paths or DEFAULT_PATHS[args.zone]
    try:
        sbom = scan(paths)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as err:
        print(f"LICENSE GATE FAILED (fail-closed): could not scan {err}")
        return 2
    bad = check(sbom, args.zone)
    if bad:
        print(f"LICENSE GATE FAILED (zone {args.zone}, fail-closed):")
        for name, lic in bad:
            print(" ", name, lic)
        return 1
    rule = "all permissive" if args.zone == "C" else "no AGPL/SSPL/BUSL/Commons Clause"
    print(f"license gate zone {args.zone}: {len(sbom['packages'])} package(s), {rule} ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
