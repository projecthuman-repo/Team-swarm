"""Cross-process multi-agent hand-off store (Lean & Precise L0.3; v4.1 T1.7).

Reviewer/verifier agents receive the diff + task spec as a bundle instead
of re-reading the repo. The bundle lives in Valkey at `handoff:<task_id>`
(TTL = MAX_TASK_WALL_S), so a verifier in ANOTHER process or container —
e.g. `python -m agent.verifier <task_id>` — reads exactly what the author's
side wrote.

Compression happens in flight, not here: the verifier sends its model
request to VERIFIER_API_BASE, which defaults to the Headroom proxy when
HEADROOM_ENABLED=1 (see agent/verifier.py). That keeps the headroom-ai
library out of the agent environment (it cannot co-resolve with aider).

Fallback: when VALKEY_URL is unset or Valkey is unreachable at import
time, the store is a per-process dict and a warning is logged — hand-off
then works only within one process.
"""

from __future__ import annotations

import logging
import os

from agent.budget import MAX_TASK_WALL_S

log = logging.getLogger("shared-context")

_plain: dict[str, str] = {}


def _connect():
    """Sync Valkey client, or None for the in-memory fallback."""
    if not os.environ.get("VALKEY_URL"):
        log.warning(
            "VALKEY_URL unset: hand-off store is per-process memory; a "
            "verifier in another process will not see bundles"
        )
        return None
    try:
        from agent.budget import sync_client

        conn = sync_client()
        conn.ping()
        return conn
    except Exception as err:  # noqa: BLE001 - any connect failure degrades
        log.warning(
            "Valkey unreachable at import (%s): hand-off store is per-process "
            "memory; a verifier in another process will not see bundles",
            err,
        )
        return None


_valkey = _connect()


def _key(task_id: str) -> str:
    return f"handoff:{task_id}"


def put_handoff(task_id: str, diff: str, spec: str) -> str:
    """Store the review bundle for a task; returns the hand-off key."""
    key = _key(task_id)
    bundle = f"# Task spec\n{spec}\n\n# Diff under review\n{diff}"
    if _valkey is not None:
        _valkey.set(key, bundle, ex=MAX_TASK_WALL_S)
    else:
        _plain[key] = bundle
    return key


def get_handoff(task_id: str) -> str | None:
    """Fetch the bundle a verifier reviews — its ONLY context (T2.3)."""
    key = _key(task_id)
    if _valkey is not None:
        value = _valkey.get(key)
        return value.decode() if isinstance(value, bytes) else value
    return _plain.get(key)
