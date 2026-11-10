"""Review R4 — dense retrieval that works where agents run.

The default embedder is an OpenAI-compatible HTTP endpoint (Ollama, vLLM,
LiteLLM); every stored vector records its embedder, and dense search only
ranks rows written by the current one.
"""

import glob
import http.server
import json
import os
import sys
import threading
import time
import uuid

import pytest
import yaml

from agent import lessons, sla_metrics
from agent.lessons import to_pgvector

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class FakeEmbeddings:
    """A local OpenAI-compatible /v1/embeddings server (the shape Ollama serves)."""

    def __init__(self):
        self.requests, self.mode, self.dim = [], "ok", 384
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                if outer.mode == "slow":
                    time.sleep(1.0)
                if outer.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                vec = [3.0, 4.0] + [0.0] * (outer.dim - 2)
                item = {"object": "embedding", "index": 0, "embedding": vec}
                data = {"object": "list", "model": body["model"], "data": [item]}
                if outer.mode == "garbage":
                    data = {"error": "model not found"}
                payload = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(monkeypatch):
    server = FakeEmbeddings()
    monkeypatch.setattr(lessons, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(lessons, "EMBEDDING_MODEL", "all-minilm")
    monkeypatch.setattr(lessons, "EMBEDDING_DIM", 384)
    monkeypatch.setattr(lessons, "EMBEDDING_API_BASE", server.base)
    yield server
    server.close()


def test_api_embedder_posts_the_openai_shape_and_normalises(api):
    vec = lessons.embed("hello world")
    assert api.requests == [("/v1/embeddings", {"model": "all-minilm", "input": "hello world"})]
    assert len(vec) == 384 and vec[:2] == pytest.approx([0.6, 0.8])  # [3, 4] L2-normalised
    assert lessons.embedder_id() == "api:all-minilm"
    assert (
        lessons.stored_embedder(vec) == "api:all-minilm" and lessons.stored_embedder(None) is None
    )


def test_api_timeout_degrades_to_text_only(api, monkeypatch, caplog):
    api.mode = "slow"
    monkeypatch.setattr(lessons, "EMBEDDING_API_TIMEOUT_S", 0.2)
    with caplog.at_level("ERROR", logger="lessons"):
        assert lessons.embed("hello") is None
    assert "embedding API" in caplog.text and "text-only" in caplog.text
    assert lessons.EMBEDDING_API_TIMEOUT_S == 0.2  # the shipped default is asserted below


def test_api_wrong_dimension_degrades_to_text_only(api, caplog):
    api.dim = 768
    with caplog.at_level("ERROR", logger="lessons"):
        assert lessons.embed("hello") is None
    assert "returned 768 dimensions" in caplog.text and "EMBEDDING_DIM is 384" in caplog.text


@pytest.mark.parametrize("mode", ["error", "garbage"])
def test_api_failure_never_raises(api, mode, caplog):
    api.mode = mode
    with caplog.at_level("ERROR", logger="lessons"):
        assert lessons.embed("hello") is None
    assert "text-only" in caplog.text


def test_api_server_down_never_raises(api, caplog):
    api.close()
    with caplog.at_level("ERROR", logger="lessons"):
        assert lessons.embed("hello") is None


def test_shipped_defaults():
    import subprocess

    code = ("import agent.lessons as l; print(l.EMBEDDING_BACKEND, l.EMBEDDING_MODEL, "
            "l.EMBEDDING_API_BASE, l.EMBEDDING_API_TIMEOUT_S)")  # fmt: skip
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith("EMBEDDING_") and k != "OLLAMA_API_BASE"}  # fmt: skip

    def run(**env):
        out = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env={**clean, **env})
        return out.decode().split()

    assert run() == ["api", "all-minilm", "http://localhost:11434/v1", "10.0"]
    assert run(OLLAMA_API_BASE="http://ollama:11434/")[2] == "http://ollama:11434/v1"
    assert run(EMBEDDING_API_BASE="http://litellm:4000")[2] == "http://litellm:4000"


# --- lessons.embedder: one vector space per embedder -------------------------


def unit(i, dim=384):
    v = [0.0] * dim
    v[i] = 1.0
    return v


async def _insert(pg, text, vec, embedder):
    await pg.execute(
        "INSERT INTO lessons(lesson_id, text, tag, embedding, embedder) "
        "VALUES($1, $2, 't', $3, $4)",
        uuid.uuid4(), text, to_pgvector(vec), embedder,
    )  # fmt: skip


async def test_dense_search_ranks_only_the_current_embedders_rows(pg, monkeypatch):
    # B's row sits exactly on the query vector; in A's space that is meaningless.
    await _insert(pg, "written by embedder B: retry budget exhausted", unit(0), "fastembed:bge")
    await _insert(pg, "written by embedder A: unrelated note", unit(5), "api:all-minilm")
    await _insert(pg, "written before migration 010: legacy note", unit(0), None)
    monkeypatch.setattr(lessons, "embed", lambda text: unit(0))
    monkeypatch.setattr(lessons, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(lessons, "EMBEDDING_MODEL", "all-minilm")

    # a query with no word in common: only the dense branch can match — and only A's row may
    out = await lessons.search_lessons(pg, "zzz-no-such-token", limit=5, tag="t")
    assert out == ["written by embedder A: unrelated note"]
    # B's row and the legacy row are still found by text
    by_text = await lessons.search_lessons(pg, "retry budget", limit=5, tag="t")
    assert "written by embedder B: retry budget exhausted" in by_text
    assert "written before migration 010: legacy note" in await lessons.search_lessons(
        pg, "legacy", limit=5, tag="t"
    )
    # seen from embedder B, the roles swap
    monkeypatch.setattr(lessons, "EMBEDDING_BACKEND", "fastembed")
    monkeypatch.setattr(lessons, "EMBEDDING_MODEL", "bge")
    assert await lessons.search_lessons(pg, "zzz-no-such-token", limit=5, tag="t") == [
        "written by embedder B: retry budget exhausted"]  # fmt: skip


async def test_write_lesson_records_the_embedder(pg, monkeypatch):
    class Pool:  # write_lesson needs pool.acquire(); a single connection will do
        def acquire(self):
            return self

        async def __aenter__(self):
            return pg

        async def __aexit__(self, *exc):
            return False

    task_id = str(uuid.uuid4())
    await pg.execute(
        "INSERT INTO tasks(task_id, role, budget_tokens) VALUES($1, 'backend', 1000)",
        uuid.UUID(task_id),
    )
    monkeypatch.setattr(lessons, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(lessons, "EMBEDDING_MODEL", "all-minilm")
    monkeypatch.setattr(lessons, "embed", lambda text: unit(1))
    with_vec = await lessons.write_lesson(Pool(), task_id, "embedded lesson", "agent-1")
    monkeypatch.setattr(lessons, "embed", lambda text: None)  # embedder down
    text_only = await lessons.write_lesson(Pool(), task_id, "text-only lesson", "agent-1")
    rows = {str(r["lesson_id"]): r for r in await pg.fetch("SELECT * FROM lessons")}
    assert rows[with_vec]["embedder"] == "api:all-minilm" and rows[with_vec]["embedding"]
    assert rows[text_only]["embedder"] is None and rows[text_only]["embedding"] is None


async def test_reembed_rewrites_the_embedder_and_skips_failures(pg, monkeypatch):
    import seed_knowledge as seed

    await _insert(pg, "old space", unit(0), "fastembed:bge")
    await _insert(pg, "legacy", unit(0), None)
    monkeypatch.setattr(seed, "embed", lambda text: unit(2))
    monkeypatch.setattr(seed, "embedder_id", lambda: "api:all-minilm")
    assert await seed.reembed(pg) == 2
    assert {r["embedder"] for r in await pg.fetch("SELECT embedder FROM lessons")} == {
        "api:all-minilm"}  # fmt: skip
    # a call that fails for one row leaves that row untouched and the run fails
    monkeypatch.setattr(seed, "embedder_id", lambda: "api:other")
    monkeypatch.setattr(seed, "embed", lambda text: None if text == "legacy" else unit(3))
    with pytest.raises(SystemExit) as err:
        await seed.reembed(pg)
    assert "1 of 2" in str(err.value)
    kept = await pg.fetchrow("SELECT embedder, embedding FROM lessons WHERE text = 'legacy'")
    assert kept["embedder"] == "api:all-minilm" and kept["embedding"] is not None


async def test_sla_lists_lessons_per_embedder(pg):
    import sla_report

    await _insert(pg, "a", unit(0), "api:all-minilm")
    await _insert(pg, "b", unit(1), "api:all-minilm")
    await _insert(pg, "c", None, None)
    counts = await sla_metrics.lessons_by_embedder(pg)
    assert counts == [("api:all-minilm", 2), ("(text-only)", 1)]
    assert sla_report.lessons_line(counts) == (
        "lessons by embedder   : api:all-minilm 2, (text-only) 1"
    )
    assert "sla_metrics.lessons_by_embedder(pool)" in read("scripts", "sla_report.py")


# --- migration 010 -----------------------------------------------------------


async def test_migration_010_is_idempotent_and_replaces_the_function(pg):
    signature = (
        "SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE p.proname = 'match_lessons_hybrid' AND n.nspname = current_schema()"
    )
    five = (
        "query_text text, query_embedding vector, current_embedder text, want_tag text, k integer"
    )
    assert [r[0] for r in await pg.fetch(signature)] == [five]  # db/schema.sql mirrors 010
    migrations = sorted(glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql")))
    assert "010_lessons_embedder.sql" in [os.path.basename(m) for m in migrations]
    await _insert(pg, "keeps its vector", unit(0), "api:all-minilm")
    for _ in range(2):  # 007 re-creates the four-argument function; 010 drops it again
        for path in migrations:
            with open(path) as f:
                await pg.execute(f.read())
    assert [r[0] for r in await pg.fetch(signature)] == [five]
    row = await pg.fetchrow("SELECT embedder, embedding FROM lessons")
    assert row["embedder"] == "api:all-minilm" and row["embedding"] is not None


# --- deployments -------------------------------------------------------------


def test_agents_embed_through_the_inference_server():
    env = yaml.safe_load(read("deploy", "compose.yaml"))["services"]["agent-backend"]["environment"]
    assert env["EMBEDDING_BACKEND"] == "api" and env["EMBEDDING_MODEL"] == "all-minilm"
    assert env["EMBEDDING_API_BASE"] == "http://ollama:11434/v1"  # the compose ollama service
    for manifest in ("agent-backend.yaml", "agent-roles.yaml"):
        for doc in yaml.safe_load_all(read("deploy", "k8s", manifest)):
            if doc and doc["kind"] == "Deployment":
                for c in doc["spec"]["template"]["spec"]["containers"]:
                    names = {e["name"]: e.get("value") for e in c.get("env", [])}
                    assert names["EMBEDDING_BACKEND"] == "api", doc["metadata"]
                    assert names["EMBEDDING_API_BASE"] == "http://embeddings:11434/v1"
    docs = [d for d in yaml.safe_load_all(read("deploy", "k8s", "embeddings.yaml")) if d]
    service = next(d for d in docs if d["kind"] == "Service")
    assert service["metadata"]["name"] == "embeddings"
    assert service["spec"]["ports"][0]["port"] == 11434
    assert "kubectl apply -f embeddings.yaml" in read("deploy", "k8s", "install.sh")


def test_make_models_pulls_the_embedding_model_and_the_real_run_is_deferred():
    makefile = read("Makefile")
    recipe = makefile.split("\nmodels:")[1].split("\n\n")[0]
    assert "ollama pull maxwell1500/ornith-9b:Q4_K_M" in recipe  # kept
    assert "ollama pull $(EMBED_MODEL)" in recipe and "EMBED_MODEL ?= all-minilm" in makefile
    vi = makefile.split("\nverify-integration:")[1]
    assert "EMBEDDING_BACKEND=api" in vi and "len(v) == 384" in vi
    shadow = read("scripts", "embeddings_shadow.sh")
    for label in ("baseline-st-minilm", "fastembed-bge-small", "api-all-minilm"):
        assert label in shadow
    assert shadow.rstrip().splitlines()[-1] == 'exit "$fail"'
    assert (
        "eval/api-all-minilm.json" in shadow.split("--report")[-1]
    )  # the default's report is last
