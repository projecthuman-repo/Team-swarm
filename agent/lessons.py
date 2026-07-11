"""Writable vector memory (pgvector) — the live learning loop.

v3.0 mounted the vector DB read-only, which defeated within-run learning;
v4.0 stores lessons in Postgres/pgvector so agents write new lessons live
and they are transactional with tasks.

Embeddings use all-MiniLM-L6-v2 (384-dim) via sentence-transformers when
installed (requirements-embeddings.txt); otherwise lessons are stored
text-only and still keyword-searchable.
"""

from __future__ import annotations

import logging
import time
import uuid

import asyncpg

log = logging.getLogger("lessons")

try:
    from sentence_transformers import SentenceTransformer

    _model: SentenceTransformer | None = SentenceTransformer("all-MiniLM-L6-v2")
except ImportError:  # embeddings optional on the laptop tier
    _model = None


def _embed(text: str) -> str | None:
    if _model is None:
        return None
    vec = _model.encode(text, normalize_embeddings=True).tolist()
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def write_lesson(
    pool: asyncpg.Pool, task_id: str, text: str, agent: str
) -> str:
    """Store a lesson and its LESSON event in one transaction (via outbox)."""
    import os  # local import keeps module import cheap
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))
    from swarm.v1 import event_pb2

    lesson_id = str(uuid.uuid4())
    ev = event_pb2.AgentEvent(
        task_id=task_id,
        agent=agent,
        kind=event_pb2.AgentEvent.LESSON,
        detail=text[:500],
        ts_unix=int(time.time()),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO lessons(lesson_id, task_id, text, embedding) "
                "VALUES($1, $2, $3, $4)",
                uuid.UUID(lesson_id),
                uuid.UUID(task_id),
                text,
                _embed(text),
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                "swarm.events.agent",
                str(uuid.uuid4()),
                ev.SerializeToString(),
            )
    return lesson_id


async def search_lessons(pool: asyncpg.Pool, query: str, limit: int = 5) -> list[str]:
    """Nearest-neighbour search over lessons (cosine, HNSW index)."""
    emb = _embed(query)
    if emb is not None:
        rows = await pool.fetch(
            "SELECT text FROM lessons ORDER BY embedding <=> $1 LIMIT $2",
            emb,
            limit,
        )
    else:  # text fallback when embeddings aren't installed
        rows = await pool.fetch(
            "SELECT text FROM lessons WHERE text ILIKE '%' || $1 || '%' "
            "ORDER BY created_at DESC LIMIT $2",
            query,
            limit,
        )
    return [r["text"] for r in rows]
