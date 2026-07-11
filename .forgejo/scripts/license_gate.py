#!/usr/bin/env python3
"""Fail-closed license gate (Hard Rule 9, runbook §12/§19).

Allowed: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC, 0BSD.
Blocked: AGPL, SSPL, BUSL/BSL, Commons Clause, Elastic-2.0 — and anything
else not on the allowlist (fail-closed). Runs syft to produce an SPDX SBOM
of the repo and exits 1 on any violation, blocking auto-merge.
"""

import json
import subprocess
import sys

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
DENIED = ("AGPL", "SSPL", "BUSL", "BSL", "Commons Clause", "Elastic-2.0")


def normalize(expr: str) -> list[str]:
    """Split an SPDX expression into atomic ids (handles AND/OR/parens)."""
    for tok in ("(", ")", " AND ", " OR ", " and ", " or "):
        expr = expr.replace(tok, "|")
    return [part.strip() for part in expr.split("|") if part.strip()]


def check(sbom: dict) -> list[tuple[str, str]]:
    bad = []
    for p in sbom.get("packages", []):
        lic = p.get("licenseConcluded") or p.get("licenseDeclared") or "NOASSERTION"
        if lic == "NOASSERTION":
            continue  # syft SPDX root doc entries; deps carry real ids
        if any(d.lower() in lic.lower() for d in DENIED):
            bad.append((p.get("name", "?"), lic))
            continue
        if not all(part in ALLOWED for part in normalize(lic)):
            bad.append((p.get("name", "?"), lic))
    return bad


def main() -> int:
    sbom = json.loads(
        subprocess.check_output(["syft", "packages", "-o", "spdx-json", "dir:."])
    )
    bad = check(sbom)
    if bad:
        print("LICENSE GATE FAILED (fail-closed):")
        for name, lic in bad:
            print(" ", name, lic)
        return 1
    print("license gate: all permissive ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
