"""L2.3 — fastembed is the default embedder; sentence-transformers stays an option."""

import os
import subprocess
import sys
import types

import pytest

from agent import lessons

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(lessons, "_model", None)
    monkeypatch.setattr(lessons, "_model_loaded", False)
    return monkeypatch


PRINT = "import agent.lessons as l; print(l.EMBEDDING_BACKEND, l.EMBEDDING_MODEL, l.EMBEDDING_DIM)"


def defaults(**env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EMBEDDING_")}
    out = subprocess.run(
        [sys.executable, "-c", PRINT],
        cwd=ROOT, env={**clean, **env}, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.split()


def test_defaults():
    assert defaults() == ["fastembed", "BAAI/bge-small-en-v1.5", "384"]
    assert defaults(EMBEDDING_BACKEND="sentence-transformers") == [
        "sentence-transformers", "all-MiniLM-L6-v2", "384"]  # fmt: skip
    assert defaults(EMBEDDING_MODEL="BAAI/bge-m3", EMBEDDING_DIM="1024")[1:] == [
        "BAAI/bge-m3", "1024"]  # fmt: skip


def fake_fastembed(dim, seen):
    mod = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name):
            seen.append(model_name)

        def embed(self, docs):
            for _ in docs:
                yield [3.0, 4.0] + [0.0] * (dim - 2)

        @staticmethod
        def list_supported_models():
            return []

    mod.TextEmbedding = TextEmbedding
    return mod


def test_fastembed_path_returns_normalized_384_vector(fresh):
    seen = []
    fresh.setitem(sys.modules, "fastembed", fake_fastembed(384, seen))
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "fastembed")
    fresh.setattr(lessons, "EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    vec = lessons.embed("hello")
    assert seen == ["BAAI/bge-small-en-v1.5"]
    assert len(vec) == 384
    assert vec[:2] == pytest.approx([0.6, 0.8])


def test_without_fastembed_lessons_stay_text_only(fresh):
    fresh.setitem(sys.modules, "fastembed", None)  # import -> ImportError
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "fastembed")
    assert lessons.embed("hello") is None
    assert lessons.to_pgvector(lessons.embed("again")) is None


def test_sentence_transformers_option_kept(fresh):
    class Model:
        def __init__(self, name):
            self.name = name

        def encode(self, text, normalize_embeddings):
            assert normalize_embeddings is True
            return types.SimpleNamespace(tolist=lambda: [0.0] * 384)

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = Model
    fresh.setitem(sys.modules, "sentence_transformers", st)
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "sentence-transformers")
    fresh.setattr(lessons, "EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    assert len(lessons.embed("hello")) == 384
    assert lessons._model.name == "all-MiniLM-L6-v2"


def test_unknown_backend_is_an_error(fresh):
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "nope")
    with pytest.raises(ValueError, match="EMBEDDING_BACKEND"):
        lessons.embed("hello")


def test_bge_m3_registers_with_the_real_fastembed_api(fresh):
    """Runs against the installed fastembed (extra `embeddings`); no download."""
    fastembed = pytest.importorskip("fastembed")
    seen = []

    def no_download(self, model_name, **kw):
        seen.append(model_name)

    fresh.setattr(fastembed.TextEmbedding, "__init__", no_download)
    fresh.setattr(lessons, "EMBEDDING_MODEL", "BAAI/bge-m3")
    lessons._fastembed_model()
    lessons._fastembed_model()  # second call must not re-register
    models = {m["model"]: m for m in fastembed.TextEmbedding.list_supported_models()}
    assert models["BAAI/bge-m3"]["dim"] == 1024
    assert seen == ["BAAI/bge-m3", "BAAI/bge-m3"]


def test_shadow_eval_require_not_worse(tmp_path):
    def run(a, b):
        pa, pb = tmp_path / "a.json", tmp_path / "b.json"
        for path, rate in ((pa, a), (pb, b)):
            path.write_text(
                f'{{"label": "{path.stem}", "tasks": 3, "green_rate": {rate}, '
                '"avg_tokens": 1, "avg_latency_s": 1, "results": []}'
            )
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "shadow_eval.py"),
             "--report", str(pa), str(pb), "--require-not-worse"],
            capture_output=True, text=True,
        )  # fmt: skip

    assert run(0.5, 0.5).returncode == 0
    assert run(0.5, 0.67).returncode == 0
    worse = run(0.67, 0.5)
    assert (
        worse.returncode == 1 and "FAIL: first-pass green rate 50% < baseline 67%" in worse.stdout
    )


def test_legacy_v41_model_id_maps_to_fastembed(fresh):
    seen = []
    fresh.setitem(sys.modules, "fastembed", fake_fastembed(384, seen))
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "fastembed")
    fresh.setattr(lessons, "EMBEDDING_MODEL", "all-MiniLM-L6-v2")  # v4.1 .env value
    assert len(lessons.embed("hello")) == 384
    assert seen == ["sentence-transformers/all-MiniLM-L6-v2"]


def test_unavailable_fastembed_model_degrades_to_text_only(fresh):
    mod = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name):
            raise ValueError(f"Could not load model {model_name} from any source.")

        @staticmethod
        def list_supported_models():
            return []

    mod.TextEmbedding = TextEmbedding
    fresh.setitem(sys.modules, "fastembed", mod)
    fresh.setattr(lessons, "EMBEDDING_BACKEND", "fastembed")
    fresh.setattr(lessons, "EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    assert lessons.embed("hello") is None


def test_legacy_alias_is_a_real_fastembed_model():
    fastembed = pytest.importorskip("fastembed")
    known = {m["model"]: m["dim"] for m in fastembed.TextEmbedding.list_supported_models()}
    for alias in lessons.FASTEMBED_ALIASES.values():
        assert known[alias] == 384
