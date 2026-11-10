"""L2.4 — multi-language, incremental codex signature index."""

import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "seed_knowledge_l24", ROOT / "scripts" / "seed_knowledge.py"
)
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def test_this_repository_indexes_the_relay_sources():
    records = seed.collect_records(ROOT)
    relay, cdc = records["relay/relay.ts"], records["relay/relay_cdc.ts"]
    assert relay.startswith("[codex-index] source: relay/relay.ts\n")
    assert "async function relayOnce(nc: NatsConnection): Promise<number>" in relay
    assert "async function sweepBacklog(nc: NatsConnection)" in cdc
    assert "publishRow(" in cdc
    assert "jetstream()" not in relay  # signatures, never bodies
    assert "agent/lessons.py" in records
    assert not any(k.startswith(("relay/node_modules", "relay/dist", "agent/gen")) for k in records)


@pytest.mark.parametrize(
    "name, src, want",
    [
        ("s.go", "package s\nfunc (s *S) Get(k string) (int, bool) {\n  return 0, true\n}\n",
         "func (s *S) Get(k string) (int, bool)"),
        ("s.rs", "impl S {\n    pub fn new(a: i32) -> Self { S { a } }\n}\n",
         "pub fn new(a: i32) -> Self"),
        ("S.java", "public class S {\n  public int get() { return 1; }\n}\n", "public int get()"),
        ("c.tsx", "export const Button = ({label}: P) => <b>{label}</b>;\n",
         "Button = ({label}: P) =>"),
        ("m.mjs", "export function f(a, b) { return a }\n", "function f(a, b)"),
    ],
)  # fmt: skip
def test_tree_sitter_languages(name, src, want):
    pytest.importorskip("tree_sitter_language_pack")
    sig = seed.extract_signatures(src, name)
    assert sig.splitlines()[0] == f"source: {name}"
    assert want in sig
    assert "return" not in sig


def test_regex_fallback_without_tree_sitter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    sig = seed.extract_signatures("export async function go(x) {\n  return x\n}\n", "a.ts")
    assert "note: regex fallback (tree-sitter-language-pack not installed)" in sig
    assert "export async function go(x)" in sig and "return" not in sig


async def count(pg, source):
    return await pg.fetchval(
        "SELECT count(*) FROM lessons WHERE tag='codex-index' AND source=$1", source
    )


async def test_incremental_reindex_in_real_postgres(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "embed", lambda text: None)
    (tmp_path / "a.py").write_text("def alpha(x):\n    return x\n")
    (tmp_path / "b.ts").write_text("export function beta(y: number) { return y }\n")
    await pg.execute(
        "INSERT INTO lessons(lesson_id, text, tag) VALUES(gen_random_uuid(), "
        "'[codex-index] file: legacy.py', 'codex-index')"
    )

    first = await seed.index_repo(pg, str(tmp_path))
    assert first == {"inserted": 2, "updated": 0, "unchanged": 0, "deleted": 1}  # legacy row
    assert await seed.index_repo(pg, str(tmp_path)) == {
        "inserted": 0, "updated": 0, "unchanged": 2, "deleted": 0}  # fmt: skip

    old_id = await pg.fetchval("SELECT lesson_id FROM lessons WHERE source='a.py'")
    (tmp_path / "a.py").write_text("def alpha(x, y):\n    return x + y\n")
    edited = await seed.index_repo(pg, str(tmp_path))
    assert edited == {"inserted": 0, "updated": 1, "unchanged": 1, "deleted": 0}
    assert await count(pg, "a.py") == 1  # replaced, zero duplicates
    row = await pg.fetchrow("SELECT lesson_id, text FROM lessons WHERE source='a.py'")
    assert row["lesson_id"] == old_id and "def alpha(x, y)" in row["text"]

    (tmp_path / "a.py").write_text("def alpha(x, y:\n")  # half-finished edit
    broken = await seed.index_repo(pg, str(tmp_path))
    assert broken["deleted"] == 0 and await count(pg, "a.py") == 1  # last good row kept
    (tmp_path / "a.py").write_text("def alpha(x, y):\n    return x + y\n")

    os.remove(tmp_path / "b.ts")
    gone = await seed.index_repo(pg, str(tmp_path))
    assert gone["deleted"] == 1 and await count(pg, "b.ts") == 0
    assert await pg.fetchval("SELECT count(*) FROM lessons WHERE tag='codex-index'") == 1
