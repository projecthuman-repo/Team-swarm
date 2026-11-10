#!/usr/bin/env python3
"""License gate — three zones (Hard Rule 9, runbook §10/§12; L3.1, review R2).

Zones (conventions/licensing.md):
  C (default)  what ships — permissive allowlist, FAIL-CLOSED: MIT,
               Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC, 0BSD (+ the
               equivalents below); anything else blocks.
  B            everything linked anywhere in the repo — fails only on the
               AGPL / SSPL / BUSL (Business Source) / Commons Clause denylist.
  A            network services — prints the services table, exits 0.

    license_gate.py [--zone C] [--python-only] [--paths agent relay ...]
    license_gate.py --zone B [--python-only] [--paths dir:.]
    license_gate.py --zone A

Python dependencies are judged from the PUBLISHER'S metadata on pypi.org for
the exact version in uv.lock (cached under .cache/license-gate/): the SPDX
`license_expression`, else the `license` field when it is a short SPDX
expression, else the `License ::` trove classifiers. Zone C applies the
allowlist to the closures of the images the swarm builds (`uv export` for
the agent, ingest and headroom images, matched by name AND version); every
other lock entry is in no image and is judged by the Zone B denylist.
`--python-only` runs just this part — no syft needed.

Everything else is scanned by syft (`dir:` / `file:` sources, one scan per
path, packages merged): the code directories and the relay's npm lock for
Zone C, the whole repository for Zone B.

Fail-closed: a metadata fetch that fails, a failed `uv export`, a path that
does not exist, a failed scan, a scan that finds no package at all, or a
lockfile syft catalogues nothing from is exit 2 — nothing passes unjudged.
A package with no declared license blocks unless a reviewed row in the
"License overrides" table names one, and that row is judged by the same
allowlist. A row can never launder: when the publisher gives no SPDX
expression and the license text or classifiers name a copyleft or
proprietary family, the package blocks whatever its row says. The one
exception is a version-pinned row in "Bundled-runtime acknowledgements"
(an unmodified GCC runtime library inside a third-party wheel).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LICENSING_MD = os.path.join(ROOT, "conventions", "licensing.md")
CACHE_DIR = os.path.join(ROOT, ".cache", "license-gate")
PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"

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
    # review R2 (owner decision): MIT-style (pillow) and the CNRI Python
    # license (regex) — both permissive, both in the agent image
    "MIT-CMU",
    "CNRI-Python",
    # DECISIONS D-036: the headroom image's closure (in scope since R2) adds
    # cffi 2.1.1 (MIT-0, MIT without the attribution clause) and numpy 2.5.4
    # (… AND Zlib …). Both OSI-approved permissive licenses.
    "MIT-0",
    "Zlib",
}
# Denied families, matched per atomic SPDX id. "BSL" alone is Business
# Source, but BSL-1.0 is the (permissive) Boost license: it is not a denied
# family — in Zone C the allowlist judges it like any other id.
DENIED_B = re.compile(r"AGPL|SSPL|BUSL|^BSL(?!-1\.0)|Commons[ -]Clause", re.I)
DENIED_C = re.compile(r"AGPL|SSPL|BUSL|^BSL(?!-1\.0)|Commons[ -]Clause|Elastic-2\.0", re.I)
# Zone B over publisher text (classifier names spell the family out).
DENIED_B_TEXT = re.compile(r"GNU Affero|Server Side Public|Business Source", re.I)

# Families an override row can never clear (review R2, "no laundering").
REFUSED = {
    "AGPL": r"AGPL|GNU Affero",
    "LGPL": r"LGPL|GNU (?:Lesser|Library)",
    "GPL": r"(?<![AL])GPL|GNU General",
    "EPL": r"\bEPL\b|Eclipse Public",
    "CDDL": r"\bCDDL",
    "EUPL": r"\bEUPL",
    "SSPL": r"\bSSPL|Server Side Public",
    "proprietary": r"proprietary",
    "non-commercial": r"non[- ]?commercial",
    "Creative Commons": r"Creative Commons|\bCC[- ]BY",
}
# The only licenses a bundled-runtime acknowledgement may carry.
ACKNOWLEDGEABLE = {"GPL-3.0-or-later WITH GCC-exception-3.1", "LGPL-2.1-or-later"}

CLASSIFIER_SPDX = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-2-Clause OR BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
}
LICENSE_FIELD_MAX = 200  # longer `license` fields are license texts, not expressions

# What each built image installs from uv.lock (the Containerfiles' selections).
IMAGES = {
    "agent": ["--no-dev", "--group", "agent", "--group", "gate"],
    "ingest": ["--only-group", "ingest"],
    "headroom": ["--only-group", "headroom-proxy"],
}

DEFAULT_PATHS = {
    "C": ["agent", "ingest", "relay", "skills", "scripts", "relay/package-lock.json"],
    "B": ["dir:."],
}

NO_LICENSE = {"", "NOASSERTION", "NONE"}
# syft writes a license text it cannot map to an SPDX id as LicenseRef-<text>.
# That is not a judged license: in Zone C it needs a reviewed override row.
FREE_TEXT = "LicenseRef-"
LOCKFILES = {"uv.lock", "package-lock.json"}
NO_LICENSE_FOUND = "no license found (add a reviewed row to 'License overrides')"


class GateError(Exception):
    """The gate cannot judge — exit 2, never a pass."""


def normalize(expr: str) -> list[str]:
    """Split an SPDX expression into atomic ids (handles AND/OR/parens)."""
    for tok in ("(", ")", " AND ", " OR ", " and ", " or "):
        expr = expr.replace(tok, "|")
    return [part.strip() for part in expr.split("|") if part.strip()]


def canonical(name: str) -> str:
    """PEP 503 name normalisation, so lock, scan and table names compare."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def _violates(lic: str, zone: str) -> bool:
    parts = normalize(lic)
    if zone == "B":
        return any(DENIED_B.search(part) for part in parts)
    if any(DENIED_C.search(part) for part in parts):
        return True
    return not all(part in ALLOWED for part in parts)


def refused_families(text: str) -> list[str]:
    """Copyleft / proprietary families named in a license text or label."""
    flat = " ".join(re.sub(r"[-_]", " ", text).split())
    hyphenated = " ".join(text.split())
    return [
        family
        for family, pattern in REFUSED.items()
        if re.search(pattern, flat, re.I) or re.search(pattern, hyphenated, re.I)
    ]


# --- licensing.md tables ----------------------------------------------------


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
    """Reviewed licenses for packages whose publisher declares none."""
    return {canonical(r["package"]): r["license"] for r in _table("License overrides", path)}


def acknowledgements(path: str = LICENSING_MD) -> dict[tuple[str, str], list[dict]]:
    """Bundled-runtime acknowledgements, keyed by (package, version)."""
    acks: dict[tuple[str, str], list[dict]] = {}
    for row in _table("Bundled-runtime acknowledgements", path):
        if row.get("license") not in ACKNOWLEDGEABLE or not row.get("bundled library"):
            raise GateError(
                f"bundled-runtime acknowledgement for {row.get('package')} carries "
                f"{row.get('license')!r}; only {sorted(ACKNOWLEDGEABLE)} with a named library"
            )
        acks.setdefault((canonical(row["package"]), row["version"]), []).append(row)
    return acks


def first_party(root: str = ROOT) -> set[str]:
    """Names of the swarm's own packages (they appear in their own lockfiles)."""
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        names = {tomllib.load(f)["project"]["name"]}
    with open(os.path.join(root, "relay", "package.json")) as f:
        names.add(json.load(f)["name"])
    return {canonical(n) for n in names}


# --- Python: publisher metadata --------------------------------------------


def locked_packages(root: str = ROOT) -> list[tuple[str, str]]:
    """(name, version) of every registry package in uv.lock."""
    with open(os.path.join(root, "uv.lock"), "rb") as f:
        packages = tomllib.load(f)["package"]
    return [(p["name"], p["version"]) for p in packages if "registry" in p.get("source", {})]


def _download(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — fixed https host
        return json.load(resp)


def fetch_metadata(name: str, version: str, cache_dir: str = CACHE_DIR) -> dict:
    """The publisher's license metadata for one locked version (cached)."""
    path = os.path.join(cache_dir, f"{canonical(name)}-{version}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    try:
        info = _download(PYPI_URL.format(name=name, version=version))["info"]
    except Exception as err:  # network, HTTP status, bad JSON — all mean "cannot judge"
        raise GateError(f"cannot read pypi.org metadata for {name} {version}: {err}") from err
    meta = {
        "license_expression": info.get("license_expression") or "",
        "license": info.get("license") or "",
        "classifiers": [c for c in info.get("classifiers") or [] if c.startswith("License ::")],
    }
    os.makedirs(cache_dir, exist_ok=True)
    with open(f"{path}.tmp", "w") as f:
        json.dump(meta, f)
    os.replace(f"{path}.tmp", path)
    return meta


def load_metadata(packages: list[tuple[str, str]], cache_dir: str = CACHE_DIR) -> dict:
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        metas = pool.map(lambda nv: fetch_metadata(nv[0], nv[1], cache_dir), packages)
        return {(canonical(n), v): m for (n, v), m in zip(packages, metas, strict=True)}


def _spdx(text: str) -> str | None:
    """`text` as a canonical SPDX expression, or None when it is not one."""
    from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

    try:
        return canonicalize_license_expression(text)
    except InvalidLicenseExpression:
        return None


def declared(meta: dict) -> tuple[str | None, str]:
    """(license expression, where it came from) per the publisher, or (None, "")."""
    if meta.get("license_expression"):
        return meta["license_expression"], "expression"
    field = (meta.get("license") or "").strip()
    if field and len(field) <= LICENSE_FIELD_MAX and (expr := _spdx(field)):
        return expr, "license field"
    labels = [c.split("::")[-1].strip() for c in meta.get("classifiers") or []]
    if labels and all(label in CLASSIFIER_SPDX for label in labels):
        mapped = [CLASSIFIER_SPDX[label] for label in labels]
        return " AND ".join(f"({m})" if " OR " in m and len(mapped) > 1 else m for m in mapped), (
            "classifiers"
        )
    return None, ""


def closures(run=subprocess.check_output) -> dict[str, set[tuple[str, str]]]:
    """What each built image installs: {(name, version)} per image, from uv.lock."""
    out = {}
    for image, selection in IMAGES.items():
        cmd = ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", *selection]
        try:
            text = run(cmd, cwd=ROOT, stderr=subprocess.DEVNULL).decode()
        except (subprocess.CalledProcessError, OSError) as err:
            raise GateError(f"`{' '.join(cmd)}` failed: {err}") from err
        pins = set()
        for line in text.splitlines():
            m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;]+)", line)
            if m:
                pins.add((canonical(m.group(1)), m.group(2)))
        if not pins:
            raise GateError(f"`{' '.join(cmd)}` listed no package")
        out[image] = pins
    return out


def judge_python(
    metas: dict,
    shipped: dict[str, set[tuple[str, str]]],
    zone: str = "C",
    overrides: dict[str, str] | None = None,
    acks: dict | None = None,
) -> tuple[list[tuple[str, str]], dict]:
    """Violations and a per-image tally for every lock entry.

    Zone C: an entry in an image closure is judged by the allowlist; any
    other entry (in no image) by the Zone B denylist. Zone B: the denylist
    for every entry.
    """
    overrides, acks = overrides or {}, acks or {}
    bad: list[tuple[str, str]] = []
    tally = {image: {} for image in shipped}
    tally["other"] = {"denylist": 0}
    acknowledged = []
    for (name, version), meta in sorted(metas.items()):
        label = f"{name} {version}"
        images = [image for image, pins in shipped.items() if (name, version) in pins]
        expr, source = declared(meta)
        labels = [c.split("::")[-1].strip() for c in meta.get("classifiers") or []]
        if zone == "B" or not images:
            tally["other"]["denylist"] += 0 if images else 1
            texts = ([expr] if expr else []) + labels
            if any(_violates(t, "B") or DENIED_B_TEXT.search(t) for t in texts):
                bad.append((label, expr or "; ".join(labels)))
            continue
        if not meta.get("license_expression"):
            families = refused_families("\n".join([meta.get("license") or "", *labels]))
            if families and (name, version) in acks:
                acknowledged.append((label, acks[(name, version)]))
            elif families:
                bad.append(
                    (label, f"publisher's license text names {', '.join(families)} — an override "
                            "row cannot clear this (see 'Bundled-runtime acknowledgements')")
                )  # fmt: skip
                continue
        if expr is None:
            expr, source = overrides.get(name), "override row"
            if expr is None:
                bad.append((label, NO_LICENSE_FOUND))
                continue
        if _violates(expr, "C"):
            bad.append((label, f"{expr} (from the publisher's {source})"))
        for image in images:
            tally[image][source] = tally[image].get(source, 0) + 1
    tally["acknowledged"] = acknowledged
    return bad, tally


def report_python(tally: dict, zone: str, total: int) -> None:
    print(f"license gate zone {zone} — Python, publisher metadata for {total} lock entries")
    for image in IMAGES:
        counts = tally.get(image) or {}
        if zone == "C" and counts:
            parts = ", ".join(f"{n} by {source}" for source, n in sorted(counts.items()))
            print(f"  {image} image: {sum(counts.values())} pins judged by the allowlist — {parts}")
    if zone == "C":
        print(f"  in no image: {tally['other']['denylist']} entries judged by the Zone B denylist")
    for label, rows in tally.get("acknowledged", []):
        libs = "; ".join(f"{r['bundled library']} ({r['license']})" for r in rows)
        print(f"  acknowledged bundled runtime: {label} — {libs}")


def python_verdicts(zone: str) -> list[tuple[str, str]]:
    packages = locked_packages()
    metas = load_metadata(packages)
    shipped = closures() if zone == "C" else {}
    bad, tally = judge_python(metas, shipped, zone, license_overrides(), acknowledgements())
    report_python(tally, zone, len(packages))
    return bad


# --- syft: code directories, the npm lock, the repository ------------------


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


def _is_root(p: dict) -> bool:
    # syft adds one package per scanned source (SPDXRef-DocumentRoot-…).
    return str(p.get("SPDXID", "")).startswith("SPDXRef-DocumentRoot")


def _free_text(lic: str) -> bool:
    """True when the expression is nothing but text syft could not map."""
    return all(part.startswith(FREE_TEXT) for part in normalize(lic))


def check(
    sbom: dict,
    zone: str = "C",
    overrides: dict[str, str] | None = None,
    first_party: frozenset[str] | set[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Violations as (package, license) for a syft SBOM. Packages are grouped
    by name and version across the scanned paths; a license found by any
    path counts.

    Zone C: a real SPDX id is always judged by the allowlist and never
    overridden. A package with no license, or only free text, blocks unless
    a reviewed override row names its license — and that is judged too.
    Free text that names a copyleft or proprietary family blocks whatever
    the row says.
    """
    groups: dict[tuple, dict] = {}
    for p in sbom.get("packages", []):
        key = (p.get("name", "?"), p.get("versionInfo"))
        group = groups.setdefault(key, {"licenses": [], "root": False})
        group["licenses"] += [lic for lic in licenses(p) if lic not in group["licenses"]]
        group["root"] = group["root"] or _is_root(p)
    bad = []
    for (name, _version), group in groups.items():
        found = group["licenses"]
        if zone == "B":  # a denylist: unknown and missing licenses pass
            bad += [(name, lic) for lic in found if _violates(lic, "B")]
            continue
        if any(not _free_text(lic) for lic in found):
            # a real SPDX id or expression was found: everything found is judged
            bad += [(name, lic) for lic in found if _violates(lic, "C")]
            continue
        # nothing, or only free text syft could not map (LicenseRef-…)
        refused = [
            lic for lic in found
            if refused_families(lic) or any(DENIED_C.search(p) for p in normalize(lic))
        ]  # fmt: skip
        if refused:
            bad += [(name, lic) for lic in refused]
        elif not found and (group["root"] or canonical(name) in first_party):
            continue  # scan roots and the swarm's own code carry no license
        elif (reviewed := (overrides or {}).get(canonical(name))) is None:
            bad += [(name, lic) for lic in found] or [(name, NO_LICENSE_FOUND)]
        elif _violates(reviewed, "C"):
            bad.append((name, reviewed))
    return bad


def source_for(path: str) -> str:
    if ":" in path:
        return path  # already a syft source (dir:., file:x, registry:...)
    full = os.path.join(ROOT, path)
    if os.path.isdir(full):
        return f"dir:{full}"
    if os.path.isfile(full):
        return f"file:{full}"
    raise FileNotFoundError(path)


# Zone B scans the whole repository, uv.lock included; uv.lock has no license
# fields, so let syft ask PyPI for them there (python.search-remote-licenses).
SYFT_ENV = {"SYFT_PYTHON_SEARCH_REMOTE_LICENSES": "true", **os.environ}


def scan(paths: list[str]) -> dict:
    """One syft scan per path, packages merged and de-duplicated.

    `empty` lists the paths syft catalogued nothing from (its own root
    entry aside)."""
    merged, seen, empty = [], set(), []
    for path in paths:
        out = subprocess.check_output(
            ["syft", "packages", "-o", "spdx-json", source_for(path)], cwd=ROOT, env=SYFT_ENV
        )
        packages = json.loads(out).get("packages", [])
        if all(_is_root(p) for p in packages):
            empty.append(path)
        for p in packages:
            key = (p.get("name"), p.get("versionInfo"), tuple(licenses(p)), _is_root(p))
            if key not in seen:
                seen.add(key)
                merged.append(p)
    return {"packages": merged, "empty": empty}


def syft_verdicts(zone: str, paths: list[str]) -> tuple[list[tuple[str, str]], int]:
    try:
        sbom = scan(paths)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as err:
        raise GateError(f"could not scan {err}") from err
    count = sum(1 for p in sbom["packages"] if not _is_root(p))  # syft's own root entries aside
    if not count:
        raise GateError("the scan found no package at all")
    empty_locks = [p for p in sbom["empty"] if os.path.basename(p) in LOCKFILES]
    if empty_locks:
        raise GateError(f"nothing catalogued from {empty_locks}")
    return check(sbom, zone, license_overrides(), first_party()), count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--zone", choices=("A", "B", "C"), default="C")
    ap.add_argument("--paths", nargs="+", help="override the zone's default syft scan paths")
    ap.add_argument(
        "--python-only",
        action="store_true",
        help="judge only the Python lock from publisher metadata (no syft)",
    )
    args = ap.parse_args(argv)
    if args.python_only and args.paths:
        ap.error("--paths selects syft scan paths; it cannot be combined with --python-only")

    if args.zone == "A":
        print("Zone A — network services (any OSI license; isolated, unmodified):")
        for row in services_table():
            print(f"  {row['service']:<22} {row['license']:<18} {row['image']}")
        return 0

    try:
        # --paths narrows the run to a syft scan of those paths
        bad = [] if args.paths else python_verdicts(args.zone)
        count = None
        if not args.python_only:
            scanned, count = syft_verdicts(args.zone, args.paths or DEFAULT_PATHS[args.zone])
            bad += scanned
    except GateError as err:
        print(f"LICENSE GATE FAILED (fail-closed): {err}")
        return 2
    if bad:
        print(f"LICENSE GATE FAILED (zone {args.zone}, fail-closed):")
        for name, lic in bad:
            print(" ", name, lic)
        if args.zone == "C" and any(lic == NO_LICENSE_FOUND or FREE_TEXT in lic for _, lic in bad):
            print("a missing or free-text (LicenseRef-…) license needs a reviewed row in")
            print("conventions/licensing.md, 'License overrides'")
        return 1
    rule = "all permissive" if args.zone == "C" else "no AGPL/SSPL/BUSL/Commons Clause"
    scope = "Python lock only (syft not run)" if count is None else f"{count} package(s) from syft"
    print(f"license gate zone {args.zone}: {scope}, {rule} ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
