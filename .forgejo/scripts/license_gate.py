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
one scan per path, and the packages merged. A path that does not exist, a
scan that fails, or a scan that finds no package at all fails the gate
(exit 2) — nothing passes unscanned.

Zone C is fail-closed on *missing* licenses too (Phase-3 review fix): a
package for which no scanned path yields a license blocks, unless it is
the swarm's own code or has a reviewed row in the "License overrides"
table of conventions/licensing.md. Every package in uv.lock must appear in
the scan. uv.lock carries no license data, so syft is run with
SYFT_PYTHON_SEARCH_REMOTE_LICENSES=true and asks pypi.org — Zone C
therefore needs pypi.org reachable (it is on the CI allow-list); offline,
it fails rather than passes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib

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


NO_LICENSE = {"", "NOASSERTION", "NONE"}
NO_LICENSE_FOUND = "no license found (add a reviewed row to 'License overrides')"


def licenses(p: dict) -> list[str]:
    """Every license expression syft recorded for a package.

    syft writes licenses read from package metadata to licenseDeclared and
    leaves licenseConcluded as the literal string NOASSERTION, so both
    fields count — reading "concluded or declared" skipped every package.
    """
    found = []
    for key in ("licenseConcluded", "licenseDeclared"):
        value = (p.get(key) or "").strip()
        if value not in NO_LICENSE and value not in found:
            found.append(value)
    return found


def canonical(name: str) -> str:
    """PEP 503 name normalisation, so lock, scan and override names compare."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def _is_root(p: dict) -> bool:
    # syft adds one package per scanned source (SPDXRef-DocumentRoot-…).
    return str(p.get("SPDXID", "")).startswith("SPDXRef-DocumentRoot")


def _violates(lic: str, zone: str) -> bool:
    if zone == "B":
        return any(DENIED_B.search(part) for part in normalize(lic))
    if any(d.lower() in lic.lower() for d in DENIED):
        return True
    return not all(part in ALLOWED for part in normalize(lic))


def check(
    sbom: dict,
    zone: str = "C",
    overrides: dict[str, str] | None = None,
    first_party: frozenset[str] | set[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Violations as (package, license). Packages are grouped by name and
    version across the scanned paths; a license found by any path counts."""
    groups: dict[tuple, dict] = {}
    for p in sbom.get("packages", []):
        key = (p.get("name", "?"), p.get("versionInfo"))
        group = groups.setdefault(key, {"licenses": [], "root": False})
        group["licenses"] += [lic for lic in licenses(p) if lic not in group["licenses"]]
        group["root"] = group["root"] or _is_root(p)
    bad = []
    for (name, _version), group in groups.items():
        found = group["licenses"]
        if not found:
            if zone == "B" or group["root"] or canonical(name) in first_party:
                continue  # B is a denylist; roots and the swarm's own code carry none
            reviewed = (overrides or {}).get(canonical(name))
            if reviewed is None:
                bad.append((name, NO_LICENSE_FOUND))
                continue
            found = [reviewed]  # an override never replaces a license that was found
        bad += [(name, lic) for lic in found if _violates(lic, zone)]
    return bad


def _table(section: str, path: str) -> list[dict]:
    """Rows of the markdown table under `## <section>` in licensing.md."""
    rows, header, inside = [], None, False
    with open(path) as f:
        for line in f:
            if line.startswith("## "):
                inside = line.strip() == f"## {section}"
                continue
            if not inside or not line.startswith("|"):
                continue
            cells = [c.replace("`", "").strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells
            elif not set(line.strip()) <= set("|-: "):
                rows.append(dict(zip(header, cells, strict=False)))
    return rows


def services_table(path: str = LICENSING_MD) -> list[dict]:
    """Rows of the '## Services (Zone A)' table in conventions/licensing.md."""
    return _table("Services (Zone A)", path)


def license_overrides(path: str = LICENSING_MD) -> dict[str, str]:
    """Reviewed licenses for packages whose metadata declares none."""
    return {canonical(r["package"]): r["license"] for r in _table("License overrides", path)}


def first_party(root: str = ROOT) -> set[str]:
    """Names of the swarm's own packages (they appear in their own lockfiles)."""
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        names = {tomllib.load(f)["project"]["name"]}
    with open(os.path.join(root, "relay", "package.json")) as f:
        names.add(json.load(f)["name"])
    return {canonical(n) for n in names}


def locked_python(root: str = ROOT) -> set[str]:
    """Canonical names of every registry package in uv.lock."""
    with open(os.path.join(root, "uv.lock"), "rb") as f:
        packages = tomllib.load(f)["package"]
    return {canonical(p["name"]) for p in packages if "registry" in p.get("source", {})}


def unscanned(sbom: dict, paths: list[str], root: str = ROOT) -> list[str]:
    """uv.lock packages the scan did not report (only when uv.lock was a path)."""
    if "uv.lock" not in paths:
        return []
    seen = {canonical(p.get("name", "")) for p in sbom.get("packages", [])}
    return sorted(locked_python(root) - seen)


def source_for(path: str) -> str:
    if ":" in path:
        return path  # already a syft source (dir:., file:x, registry:...)
    full = os.path.join(ROOT, path)
    if os.path.isdir(full):
        return f"dir:{full}"
    if os.path.isfile(full):
        return f"file:{full}"
    raise FileNotFoundError(path)


# uv.lock has no license fields; let syft ask PyPI for them (python.search-remote-licenses).
SYFT_ENV = {"SYFT_PYTHON_SEARCH_REMOTE_LICENSES": "true", **os.environ}


def scan(paths: list[str]) -> dict:
    """One syft scan per path, packages merged and de-duplicated."""
    merged, seen = [], set()
    for path in paths:
        out = subprocess.check_output(
            ["syft", "packages", "-o", "spdx-json", source_for(path)], cwd=ROOT, env=SYFT_ENV
        )
        for p in json.loads(out).get("packages", []):
            key = (p.get("name"), p.get("versionInfo"), tuple(licenses(p)), _is_root(p))
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
    if not sbom["packages"]:
        print("LICENSE GATE FAILED (fail-closed): the scan found no package at all")
        return 2
    bad = check(sbom, args.zone, license_overrides(), first_party())
    if args.zone == "C":
        bad += [(name, "locked in uv.lock but not in the scan") for name in unscanned(sbom, paths)]
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
