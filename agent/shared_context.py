"""Multi-agent hand-off via Headroom SharedContext (v4.1 T1.7).

Reviewer/verifier agents receive the diff + task spec through a shared,
compressed context store instead of re-reading the repo — cutting
reviewer context tokens (target: >=30% lower on fixtures with unchanged
verdicts). Without headroom-ai installed, hand-off falls back to a plain
in-process store so the pipeline still runs (compression becomes a no-op).
"""

from __future__ import annotations

import logging

log = logging.getLogger("shared-context")

_store = None
_plain: dict[str, str] = {}


def _shared():
    global _store
    if _store is None:
        try:
            import headroom

            _store = headroom.SharedContext(model="gpt-4o")
        except ImportError:
            _store = False  # sentinel: plain fallback
    return _store


def compress_text(text: str) -> str:
    """Compress a hand-off blob with Headroom; identity without it."""
    try:
        import headroom

        res = headroom.compress([{"role": "tool", "content": text}], model="gpt-4o")
        return str(res.messages[-1]["content"])
    except ImportError:
        return text
    except Exception as err:  # hand-off must never fail on compression
        log.warning("compress failed, passing through: %s", err)
        return text


def put_handoff(task_id: str, diff: str, spec: str) -> str:
    """Store the review bundle for a task; returns the hand-off key."""
    key = f"handoff:{task_id}"
    bundle = f"# Task spec\n{spec}\n\n# Diff under review\n{diff}"
    store = _shared()
    if store:
        store.put(key, compress_text(bundle))
    else:
        _plain[key] = bundle
    return key


def get_handoff(task_id: str) -> str | None:
    """Fetch the bundle a verifier reviews — its ONLY context (T2.3)."""
    key = f"handoff:{task_id}"
    store = _shared()
    if store:
        return store.get(key)
    return _plain.get(key)
