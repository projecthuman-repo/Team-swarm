"""Library-mode compression for oversized ingest bodies (v4.1 T1.8).

Issue bodies / pasted logs above the threshold go through Headroom's
SmartCrusher before task-spec storage; the ORIGINAL is stored in
SeaweedFS and referenced via the existing spec_ref field, so
reversibility is preserved (nothing is lost, only deferred).

SeaweedFS is addressed through its filer HTTP API (simple PUT/GET, no
S3 signing needed inside the mesh). Headroom is optional: without it,
oversized bodies are still offloaded and a plain head-excerpt is used.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("compress-util")

SEAWEED_FILER_URL = os.environ.get("SEAWEED_FILER_URL", "http://seaweedfs:8888")
COMPRESS_THRESHOLD_CHARS = int(os.environ.get("COMPRESS_THRESHOLD_CHARS", "8000"))
SPEC_BUCKET = "specs"


def headroom_compress_text(text: str) -> str | None:
    """SmartCrusher a text blob; None if headroom-ai isn't installed."""
    try:
        import headroom
    except ImportError:
        return None
    try:
        res = headroom.compress(
            [{"role": "tool", "content": text}], model="gpt-4o"
        )
        return str(res.messages[-1]["content"])
    except Exception as err:  # compression must never block ingestion
        log.warning("headroom compress failed, falling back: %s", err)
        return None


def offload_original(key: str, text: str) -> str | None:
    """PUT the original body to SeaweedFS; returns the spec_ref key."""
    ref = f"{SPEC_BUCKET}/{key}.txt"
    try:
        resp = requests.put(
            f"{SEAWEED_FILER_URL.rstrip('/')}/{ref}",
            data=text.encode(),
            timeout=15,
        )
        resp.raise_for_status()
        return ref
    except requests.RequestException as err:
        log.warning("seaweedfs offload failed (%s); keeping inline", err)
        return None


def fetch_original(spec_ref: str) -> str:
    """GET an offloaded original back from SeaweedFS (reversibility)."""
    resp = requests.get(
        f"{SEAWEED_FILER_URL.rstrip('/')}/{spec_ref}", timeout=15
    )
    resp.raise_for_status()
    return resp.text


def prepare_spec(task_id: str, body: str) -> tuple[str, str]:
    """(spec text to store on the task, spec_ref) for an ingest body.

    Small bodies pass through unchanged. Oversized bodies are offloaded
    to SeaweedFS (spec_ref) and replaced by a compressed rendition
    (SmartCrusher when available, head-excerpt otherwise).
    """
    body = body or ""
    if len(body) <= COMPRESS_THRESHOLD_CHARS:
        return body, ""
    ref = offload_original(task_id, body)
    compressed = headroom_compress_text(body)
    if compressed is None:
        compressed = body[:COMPRESS_THRESHOLD_CHARS] + "\n...[truncated]"
    if ref:
        compressed += f"\n\n[original: {ref}]"
    return compressed, ref or ""
