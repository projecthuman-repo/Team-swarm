"""Writable vector memory — the live learning loop.

Postgres/pgvector is the canonical store (lessons are transactional with
tasks). Two documented alternatives are selectable via VECTOR_BACKEND:

  pgvector (baseline)   — search runs in Postgres: HNSW cosine + full-text,
                          fused by RRF (match_lessons_hybrid, L2.2)
  qdrant   (upgrade)    — adopt past ~5-10M vectors / heavy filtered search
  chroma   (local tier) — the retained v3.0 laptop retrieval option

With qdrant/chroma the lesson row still lands in Postgres (source of
truth); the alternative backend mirrors it as the search index.

Embeddings (EMBEDDING_BACKEND / EMBEDDING_MODEL / EMBEDDING_DIM):
  fastembed (default, L2.3 — ONNX Runtime, no torch; extra `embeddings`)
    BAAI/bge-small-en-v1.5 (default, 384-dim — db/schema.sql)
    BAAI/bge-m3 (EMBEDDING_DIM=1024 — apply db/migrations/optional/001_bge_m3_embeddings.sql;
    adopt when retrieval quality > speed)
  sentence-transformers (option; extra `embeddings-torch`, pulls torch)
    all-MiniLM-L6-v2 (its default, 384-dim) or any ST model id
Switching backend or model changes the vector space: re-embed with
`python scripts/seed_knowledge.py --reembed`. Without the selected
library, lessons are stored text-only and full-text searchable.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import asyncpg

log = logging.getLogger("lessons")

BACKEND = os.environ.get("VECTOR_BACKEND", "pgvector")  # pgvector|qdrant|chroma
EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "fastembed")  # |sentence-transformers
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL") or (
    "all-MiniLM-L6-v2" if EMBEDDING_BACKEND == "sentence-transformers" else "BAAI/bge-small-en-v1.5"
)
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
CHROMA_PATH = os.environ.get("CHROMA_PATH", "/tmp/chroma")  # nosec B108 - dev default, override in prod
COLLECTION = "lessons"

_model = None
_model_loaded = False


# v4.1 configs set EMBEDDING_MODEL=all-MiniLM-L6-v2 explicitly; fastembed
# ships the same model (ONNX export) under its hub id. Same model, same
# vector space — existing lessons keep matching without a re-embed.
FASTEMBED_ALIASES = {"all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2"}


def _fastembed_model():
    """fastembed TextEmbedding; BAAI/bge-m3 is registered from its ONNX export."""
    from fastembed import TextEmbedding

    if EMBEDDING_MODEL.lower() == "baai/bge-m3":
        from fastembed.common.model_description import ModelSource, PoolingType

        known = {m["model"].lower() for m in TextEmbedding.list_supported_models()}
        if "baai/bge-m3" not in known:
            TextEmbedding.add_custom_model(
                model="BAAI/bge-m3",
                pooling=PoolingType.CLS,  # bge-m3 dense = normalized [CLS]
                normalization=True,
                sources=ModelSource(hf="BAAI/bge-m3"),
                dim=1024,
                model_file="onnx/model.onnx",
                additional_files=["onnx/model.onnx_data"],
            )
    return TextEmbedding(model_name=FASTEMBED_ALIASES.get(EMBEDDING_MODEL, EMBEDDING_MODEL))


def _embedder():
    global _model, _model_loaded
    if not _model_loaded:
        _model_loaded = True
        if EMBEDDING_BACKEND == "sentence-transformers":
            try:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(EMBEDDING_MODEL)
            except ImportError:  # embeddings optional on the laptop tier
                log.warning("sentence-transformers not installed; text-only lessons")
        elif EMBEDDING_BACKEND == "fastembed":
            try:
                _model = _fastembed_model()
            except ImportError:  # embeddings optional on the laptop tier
                log.warning("fastembed not installed; text-only lessons")
            except Exception as err:  # unknown model id, model not cached + no egress
                log.error(
                    "fastembed model %r unavailable (%s); text-only lessons", EMBEDDING_MODEL, err
                )
        else:
            raise ValueError(f"unknown EMBEDDING_BACKEND {EMBEDDING_BACKEND!r}")
    return _model


def embed(text: str) -> list[float] | None:
    model = _embedder()
    if model is None:
        return None
    if EMBEDDING_BACKEND == "sentence-transformers":
        return model.encode(text, normalize_embeddings=True).tolist()
    vec = [float(x) for x in next(iter(model.embed([text])))]
    norm = sum(x * x for x in vec) ** 0.5 or 1.0  # cosine-ready, like ST's normalize
    return [x / norm for x in vec]


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

    return chromadb.PersistentClient(path=CHROMA_PATH).get_or_create_collection(COLLECTION)


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


async def write_lesson(
    pool: asyncpg.Pool,
    task_id: str,
    text: str,
    agent: str,
    tag: str | None = None,
) -> str:
    """Store a lesson and its LESSON event in one transaction (via outbox).

    `tag` drives the retrieval flywheel (v4.1 T0.2): 'headroom-learn',
    'morning-audit', 'codex-index', or a role name.
    """
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
                "INSERT INTO lessons(lesson_id, task_id, text, tag, embedding) "
                "VALUES($1, $2, $3, $4, $5)",
                uuid.UUID(lesson_id),
                uuid.UUID(task_id),
                text,
                tag,
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


async def search_lessons(
    pool: asyncpg.Pool,
    query: str,
    limit: int = 5,
    tag: str | None = None,
) -> list[str]:
    """Search lessons via the selected backend.

    pgvector (canonical, L2.2): hybrid retrieval — `match_lessons_hybrid`
    fuses the dense (embedding) and sparse (full-text) rankings with RRF
    whenever an embedding is available; without one, the sparse branch
    alone. `tag` filters to one lesson family. Qdrant/Chroma mirrors keep
    their dense path for untagged queries; tagged retrieval always goes
    through Postgres (they return untagged results).
    """
    vec = embed(query)
    if vec is not None and tag is None and BACKEND == "qdrant":
        hits = _qdrant().query_points(COLLECTION, query=vec, limit=limit).points
        return [h.payload["text"] for h in hits]
    if vec is not None and tag is None and BACKEND == "chroma":
        res = _chroma().query(query_embeddings=[vec], n_results=limit)
        return res["documents"][0] if res["documents"] else []

    if vec is not None:
        rows = await pool.fetch(
            "SELECT lesson_text AS text FROM match_lessons_hybrid($1, $2, $3, $4)",
            query,
            to_pgvector(vec),
            tag,
            limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT text FROM lessons "
            "WHERE fts @@ websearch_to_tsquery('simple', $1) "
            "AND ($3::text IS NULL OR tag = $3) "
            "ORDER BY ts_rank_cd(fts, websearch_to_tsquery('simple', $1)) DESC LIMIT $2",
            query,
            limit,
            tag,
        )
    return [r["text"] for r in rows]
