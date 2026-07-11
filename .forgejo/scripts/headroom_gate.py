#!/usr/bin/env python3
"""Headroom CI gate (v4.1 T1.1 — ledger S1 substitution).

Replaces the old gglucass/headroom-desktop step (a paid macOS tray app —
the wrong project; token compression is chopratejas/headroom, PyPI
`headroom-ai`, Apache-2.0). Runs headless on the Linux CI runner:

  (a) compresses the checked-in JSON tool-output fixture and asserts the
      compression ratio meets a floor;
  (b) asserts retrieval round-trips: sampled original values survive in
      the compressed rendition (SmartCrusher is reversible/CCR — values
      are restructured, never dropped);
  (c) asserts code content is protected (never lossily crushed).

Negative test (verifies the gate can fail): --negative corrupts the
compressed output before the round-trip check and must exit non-zero.
"""

from __future__ import annotations

import json
import os
import sys

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
RATIO_FLOOR = float(os.environ.get("HEADROOM_GATE_RATIO_FLOOR", "0.25"))


def gate(negative: bool = False) -> int:
    import headroom

    # (a) compression ratio floor on the JSON tool-output fixture
    with open(os.path.join(FIXTURES, "tool_output.json")) as f:
        blob = f.read()
    rows = json.loads(blob)
    res = headroom.compress(
        [
            {"role": "user", "content": "summarize the accounts"},
            {"role": "tool", "content": blob},
        ],
        model="gpt-4o",
    )
    saved = 1 - (res.tokens_after / max(res.tokens_before, 1))
    print(
        f"json fixture: {res.tokens_before} -> {res.tokens_after} tokens "
        f"({saved:.0%} saved; floor {RATIO_FLOOR:.0%}; "
        f"transforms {res.transforms_applied})"
    )
    if saved < RATIO_FLOOR:
        print("HEADROOM GATE FAILED: compression below floor")
        return 1

    # (b) round-trip: sampled original values survive compression
    compressed = json.dumps([m.get("content", "") for m in res.messages])
    if negative:
        compressed = compressed.replace("user", "corrupted")  # simulate loss
    for i in range(0, len(rows), 100):
        needle = rows[i]["email"]
        if needle not in compressed:
            print(f"HEADROOM GATE FAILED: value {needle!r} lost in compression")
            return 1
    print(f"round-trip: {len(range(0, len(rows), 100))} sampled values retrieved")

    # (c) code is protected content — never lossily crushed
    with open(os.path.join(FIXTURES, "sample_code.py")) as f:
        code = f.read()
    res_code = headroom.compress(
        [
            {"role": "user", "content": "review this file"},
            {"role": "user", "content": code},
        ],
        model="gpt-4o",
    )
    joined = json.dumps([m.get("content", "") for m in res_code.messages])
    if "MAGIC_SENTINEL" not in joined or "swarm-gate-fixture-7f3a9c" not in joined:
        print("HEADROOM GATE FAILED: code content was dropped")
        return 1
    print("code fixture: content protected ✓")
    print("headroom gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(gate(negative="--negative" in sys.argv))
