"""Writable vector memory — the live learning loop.

Postgres/pgvector is the canonical store (lessons are transactional with
tasks). Two documented alternatives are selectable via VECTOR_BACKEND:

  pgvector (baseline)   — search runs in Postgres (HNSW, cosine)
  qdrant   (upgrade)    — adopt past ~5-10M vectors / heavy filtered search
  chroma   (local tier) — the retained v3.0 laptop retrieval option

With qdrant/chroma the lesson row still lands in Postgres (source of
truth); the alternative backend mirrors it as the search index.

Embeddings (EMBEDDING_MODEL / EMBEDDING_DIM):
  all-MiniLM-L6-v2 (default, 384-dim — db/schema.sql)
  BAAI/bge-m3 (1024-dim — apply db/migrations/001_bge_m3_embeddings.sql;
  adopt when retrieval quality > speed)
Install requirements-embeddings.txt; without it, lessons are stored
text-only and keyword-searchable.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import asyncpg

log = logging.getLogger("lessons")

BACKEND = os.environ.get("VECTOR_BACKEND", "pgvector")  # pgvector|qdrant|chroma
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
CHROMA_PATH = os.environ.get("CHROMA_PATH", "/tmp/chroma")  # nosec B108 - dev default, override in prod
COLLECTION = "lessons"

_model = None
_model_loaded = False


def _embedder():
    global _model, _model_loaded
    if not _model_loaded:
        _model_loaded = True
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(EMBEDDING_MODEL)
        except ImportError:  # embeddings optional on the laptop tier
            log.warning("sentence-transformers not installed; text-only lessons")
    return _model


def embed(text: str) -> list[float] | None:
    model = _embedder()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True).tolist()


def to_pgvector(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# ---- alternative search backends -------------------------------------


def _qdrant():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=QDRANT_URL)
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
    return client


def _chroma():
    import chromadb

    return chromadb.PersistentClient(path=CHROMA_PATH).get_or_create_collection(
        COLLECTION
    )


def _index_upsert(lesson_id: str, text: str, vec: list[float] | None) -> None:
    if vec is None or BACKEND == "pgvector":
        return
    if BACKEND == "qdrant":
        from qdrant_client.models import PointStruct

        _qdrant().upsert(
            COLLECTION,
            [PointStruct(id=lesson_id, vector=vec, payload={"text": text})],
        )
    elif BACKEND == "chroma":
        _chroma().upsert(ids=[lesson_id], embeddings=[vec], documents=[text])


# ---- public API -------------------------------------------------------


async def write_lesson(pool: asyncpg.Pool, task_id: str, text: str, agent: str) -> str:
    """Store a lesson and its LESSON event in one transaction (via outbox)."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))
    from swarm.v1 import event_pb2

    lesson_id = str(uuid.uuid4())
    vec = embed(text)
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
                to_pgvector(vec),
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                "swarm.events.agent",
                str(uuid.uuid4()),
                ev.SerializeToString(),
            )
    _index_upsert(lesson_id, text, vec)  # mirror to qdrant/chroma if selected
    return lesson_id


async def search_lessons(pool: asyncpg.Pool, query: str, limit: int = 5) -> list[str]:
    """Nearest-neighbour search over lessons via the selected backend."""
    vec = embed(query)
    if vec is None:  # text fallback when embeddings aren't installed
        rows = await pool.fetch(
            "SELECT text FROM lessons WHERE text ILIKE '%' || $1 || '%' "
            "ORDER BY created_at DESC LIMIT $2",
            query,
            limit,
        )
        return [r["text"] for r in rows]

    if BACKEND == "qdrant":
        hits = _qdrant().query_points(COLLECTION, query=vec, limit=limit).points
        return [h.payload["text"] for h in hits]
    if BACKEND == "chroma":
        res = _chroma().query(query_embeddings=[vec], n_results=limit)
        return res["documents"][0] if res["documents"] else []

    rows = await pool.fetch(
        "SELECT text FROM lessons ORDER BY embedding <=> $1 LIMIT $2",
        to_pgvector(vec),
        limit,
    )
    return [r["text"] for r in rows]
