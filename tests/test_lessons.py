"""Vector-memory helpers: pgvector literals and knowledge chunking."""

import importlib.util
import os

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
