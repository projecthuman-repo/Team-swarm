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
  api (default, review R4 — works inside the agent containers, which carry
    no embedding library and have no egress to fetch a model):
    POST ${EMBEDDING_API_BASE}/embeddings, the OpenAI-compatible shape
    Ollama, vLLM and LiteLLM serve. Default base ${OLLAMA_API_BASE}/v1,
    default model `all-minilm` (384-dim; `make models` pulls it).
  fastembed (option, L2.3 — in-process ONNX Runtime, no torch; extra `embeddings`)
    BAAI/bge-small-en-v1.5 (its default, 384-dim)
    BAAI/bge-m3 (EMBEDDING_DIM=1024 — apply db/migrations/optional/001_bge_m3_embeddings.sql;
    adopt when retrieval quality > speed)
  sentence-transformers (option; extra `embeddings-torch`, pulls torch)
    all-MiniLM-L6-v2 (its default, 384-dim) or any ST model id
Every vector is stored with its embedder (`<backend>:<model>`, column
`lessons.embedder`); dense search ranks only rows written by the current
embedder, so rows from another vector space fall back to full-text instead
of polluting the ranking. Switching backend or model: re-embed with
`python scripts/seed_knowledge.py --reembed`. When the embedder is
unavailable, lessons are stored text-only and stay full-text searchable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

import asyncpg

log = logging.getLogger("lessons")

BACKEND = os.environ.get("VECTOR_BACKEND", "pgvector")  # pgvector|qdrant|chroma
EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "api")  # |fastembed|sentence-transformers
DEFAULT_MODELS = {
    "api": "all-minilm",
    "fastembed": "BAAI/bge-small-en-v1.5",
    "sentence-transformers": "all-MiniLM-L6-v2",
}
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODELS.get(EMBEDDING_BACKEND, "")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
# OpenAI-compatible embeddings endpoint (api backend). Default: the swarm's
# own inference server — self-hosted, on the internal network (INV-027).
EMBEDDING_API_BASE = (
    os.environ.get("EMBEDDING_API_BASE")
    or os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/") + "/v1"
)
EMBEDDING_API_TIMEOUT_S = 10.0
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


def embedder_id() -> str:
    """`<backend>:<model>` — the vector space a stored embedding belongs to."""
    return f"{EMBEDDING_BACKEND}:{EMBEDDING_MODEL}"


def _embed_api(text: str) -> list[float] | None:
    """One vector from the OpenAI-compatible endpoint, or None (text-only)."""
    import requests

    url = f"{EMBEDDING_API_BASE.rstrip('/')}/embeddings"
    try:
        resp = requests.post(
            url, json={"model": EMBEDDING_MODEL, "input": text}, timeout=EMBEDDING_API_TIMEOUT_S
        )
        resp.raise_for_status()
        vec = [float(x) for x in resp.json()["data"][0]["embedding"]]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        log.error("embedding API %s failed (%s); text-only lesson", url, err)
        return None
    if len(vec) != EMBEDDING_DIM:
        log.error(
            "embedding API %s returned %d dimensions for model %r, EMBEDDING_DIM is %d; "
            "text-only lesson",
            url, len(vec), EMBEDDING_MODEL, EMBEDDING_DIM,
        )  # fmt: skip
        return None
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


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
    """A unit-length vector for `text`, or None when the embedder is
    unavailable (the caller stores or searches text-only). Never raises for
    an unavailable embedder — a lesson must not fail a task."""
    if EMBEDDING_BACKEND == "api":
        return _embed_api(text)
    model = _embedder()
    if model is None:
        return None
    if EMBEDDING_BACKEND == "sentence-transformers":
        return model.encode(text, normalize_embeddings=True).tolist()
    vec = [float(x) for x in next(iter(model.embed([text])))]
    norm = sum(x * x for x in vec) ** 0.5 or 1.0  # cosine-ready, like ST's normalize
    return [x / norm for x in vec]


def stored_embedder(vec: list[float] | None) -> str | None:
    """Value for lessons.embedder next to `vec`: the embedder, or NULL text-only."""
    return embedder_id() if vec is not None else None


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
    vec = await asyncio.to_thread(embed, text)  # the api backend blocks on HTTP
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
                "INSERT INTO lessons(lesson_id, task_id, text, tag, embedding, embedder) "
                "VALUES($1, $2, $3, $4, $5, $6)",
                uuid.UUID(lesson_id),
                uuid.UUID(task_id),
                text,
                tag,
                to_pgvector(vec),
                stored_embedder(vec),
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
    alone. The dense branch ranks only rows written by the current
    embedder (R4). `tag` filters to one lesson family. Qdrant/Chroma mirrors keep
    their dense path for untagged queries; tagged retrieval always goes
    through Postgres (they return untagged results).
    """
    vec = await asyncio.to_thread(embed, query)
    if vec is not None and tag is None and BACKEND == "qdrant":
        hits = _qdrant().query_points(COLLECTION, query=vec, limit=limit).points
        return [h.payload["text"] for h in hits]
    if vec is not None and tag is None and BACKEND == "chroma":
        res = _chroma().query(query_embeddings=[vec], n_results=limit)
        return res["documents"][0] if res["documents"] else []

    if vec is not None:
        rows = await pool.fetch(
            "SELECT lesson_text AS text FROM match_lessons_hybrid($1, $2, $3, $4, $5)",
            query,
            to_pgvector(vec),
            embedder_id(),
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
