"""Vector-memory helpers: pgvector literals, knowledge chunking, hybrid search."""

import importlib.util
import os
import uuid

from agent import lessons
from agent.lessons import to_pgvector

spec = importlib.util.spec_from_file_location(
    "seed_knowledge",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "seed_knowledge.py"),
)
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def test_to_pgvector_literal():
    assert to_pgvector([0.5, -1.0]) == "[0.500000,-1.000000]"


def test_to_pgvector_none_passthrough():
    assert to_pgvector(None) is None


def test_chunks_preserve_paragraphs():
    text = "\n\n".join(f"para {i} " + "x" * 300 for i in range(10))
    out = seed.chunks(text)
    assert len(out) > 1
    assert all(len(c) <= seed.CHUNK_CHARS + 400 for c in out)
    assert "".join(out).count("para") == 10


def test_chunks_short_text_single():
    assert seed.chunks("hello world") == ["hello world"]


# --- Lean & Precise L2.2 — hybrid retrieval ---------------------------------


class RecordingPool:
    def __init__(self, rows=()):
        self.calls = []
        self.rows = list(rows)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


async def test_search_uses_hybrid_function_with_tag(monkeypatch):
    monkeypatch.setattr(lessons, "embed", lambda text: [0.1] * 384)
    pool = RecordingPool([{"text": "lesson"}])
    out = await lessons.search_lessons(pool, "parse_outbox_row", limit=3, tag="codex-index")
    assert out == ["lesson"]
    sql, args = pool.calls[0]
    assert "match_lessons_hybrid($1, $2, $3, $4)" in sql
    assert args[0] == "parse_outbox_row" and args[2:] == ("codex-index", 3)
    assert args[1].startswith("[0.100000,")


async def test_search_without_embedding_is_sparse_only(monkeypatch):
    monkeypatch.setattr(lessons, "embed", lambda text: None)
    pool = RecordingPool()
    await lessons.search_lessons(pool, "E1234 timeout", limit=2, tag=None)
    sql, args = pool.calls[0]
    assert "fts @@ websearch_to_tsquery('simple', $1)" in sql
    assert "match_lessons_hybrid" not in sql and "<=>" not in sql
    assert args == ("E1234 timeout", 2, None)


async def test_qdrant_mirror_keeps_dense_path(monkeypatch):
    monkeypatch.setattr(lessons, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(lessons, "BACKEND", "qdrant")

    class Hit:
        payload = {"text": "dense hit"}

    class Q:
        def query_points(self, coll, query, limit):
            return type("R", (), {"points": [Hit()]})()

    monkeypatch.setattr(lessons, "_qdrant", lambda: Q())
    pool = RecordingPool()
    assert await lessons.search_lessons(pool, "anything") == ["dense hit"]
    assert pool.calls == []
    await lessons.search_lessons(pool, "anything", tag="backend")  # tagged -> Postgres
    assert "match_lessons_hybrid" in pool.calls[0][0]


def unit(i, dim=384):
    v = [0.0] * dim
    v[i] = 1.0
    return v


async def test_exact_identifier_ranks_first_in_real_postgres(pg, monkeypatch):
    """Dense alone ranks the 'near' lesson first; RRF lifts the exact identifier."""
    rows = [
        ("near: retry the outbox relay on a dropped connection", unit(0), "backend"),
        ("parse_outbox_row must reject payloads with no msg_id", unit(1), "backend"),
        ("unrelated: frontend button colours", unit(2), "backend"),
        ("parse_outbox_row in a different family", unit(1), "review"),
    ]
    for text, vec, tag in rows:
        await pg.execute(
            "INSERT INTO lessons(lesson_id, text, tag, embedding) VALUES($1, $2, $3, $4)",
            uuid.uuid4(), text, tag, to_pgvector(vec),
        )  # fmt: skip
    query_vec = [0.9, 0.3] + [0.0] * 382  # dense: 'near' (dim 0) beats the exact one
    dense = await pg.fetch(
        "SELECT text FROM lessons WHERE tag='backend' ORDER BY embedding <=> $1 LIMIT 1",
        to_pgvector(query_vec),
    )
    assert dense[0]["text"].startswith("near:")

    monkeypatch.setattr(lessons, "embed", lambda text: query_vec)
    out = await lessons.search_lessons(pg, "parse_outbox_row", limit=3, tag="backend")
    assert out[0].startswith("parse_outbox_row must reject")
    assert all("different family" not in t for t in out)

    monkeypatch.setattr(lessons, "embed", lambda text: None)  # text-only lessons
    out = await lessons.search_lessons(pg, "parse_outbox_row", limit=3, tag="backend")
    assert out == ["parse_outbox_row must reject payloads with no msg_id"]
