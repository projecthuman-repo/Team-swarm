#!/usr/bin/env python3
"""AGENTS.md size gate (v4.2 T7.5).

The shared cross-tool constitution must stay <= Codex's
project_doc_max_bytes (32 KiB), or Codex truncates it and engines diverge.
Fail-closed: over the cap exits 1 and blocks the merge.
"""

import os
import sys

MAX_BYTES = int(os.environ.get("AGENTS_MD_MAX_BYTES", str(32 * 1024)))
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def main() -> int:
    path = os.path.join(ROOT, "AGENTS.md")
    size = os.path.getsize(path)
    print(f"AGENTS.md: {size} bytes (cap {MAX_BYTES})")
    if size > MAX_BYTES:
        print(
            "AGENTS.MD GATE FAILED: over 32 KiB — Codex will truncate the "
            "constitution and engines will diverge (T7.5). Move detail into "
            "conventions/ and reference it instead."
        )
        return 1
    print("agents-md gate: within cap ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
