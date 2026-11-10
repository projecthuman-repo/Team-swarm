"""Seed vector memory with upstream knowledge packs (Appendix C).

Chunks every .md/.txt file under knowledge/ into lessons (task_id NULL)
with embeddings, so agents retrieve platform know-how from their first
task. Also supports re-embedding after an embedding-model swap (e.g. the
bge-m3 upgrade) and relevance spot-checks (the §17 SLA).

v4.1 T2.4 — repository RAG done right: --index-repo walks a repo and
indexes FILES + API SIGNATURES (AST) into the codex knowledge pack
(lessons tag='codex-index'). Deliberately excluded: "similar-snippet"
retrieval — it empirically degrades results up to 15%. Retrieval is
injected as bounded context (agent/context_pack.py) under a hard token
cap.

Usage:
    python scripts/seed_knowledge.py                 # ingest knowledge/
    python scripts/seed_knowledge.py --reembed       # after model swap
    python scripts/seed_knowledge.py --query "jetstream ack"   # spot-check
    python scripts/seed_knowledge.py --index-repo /path/to/repo
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import pathlib
import re
import sys
import uuid

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.lessons import (  # noqa: E402
    embed,
    embedder_id,
    search_lessons,
    stored_embedder,
    to_pgvector,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm")
KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "knowledge"
CHUNK_CHARS = 1200


def chunks(text: str) -> list[str]:
    """Paragraph-preserving chunks of ~CHUNK_CHARS."""
    out: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) > CHUNK_CHARS and buf:
            out.append(buf.strip())
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        out.append(buf.strip())
    return out


async def seed(pool: asyncpg.Pool) -> int:
    n = 0
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        if path.suffix not in {".md", ".txt"} or path.name == "README.md":
            continue
        for chunk in chunks(path.read_text(errors="replace")):
            text = f"[{path.relative_to(KNOWLEDGE_DIR)}] {chunk}"
            exists = await pool.fetchval("SELECT 1 FROM lessons WHERE text = $1", text)
            if exists:
                continue
            vec = embed(text)
            await pool.execute(
                "INSERT INTO lessons(lesson_id, text, embedding, embedder) VALUES($1, $2, $3, $4)",
                uuid.uuid4(),
                text,
                to_pgvector(vec),
                stored_embedder(vec),
            )
            n += 1
    return n


TS_LANG = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
INDEX_SUFFIXES = {".py", *TS_LANG}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "gen", "dist", "__pycache__"}

# Definition nodes per tree-sitter grammar (L2.4). Function-like nodes are
# reported but never descended into: signatures, never bodies.
_DEFS = {
    "function_declaration", "generator_function_declaration", "class_declaration",
    "abstract_class_declaration", "interface_declaration", "type_alias_declaration",
    "enum_declaration", "method_definition", "method_signature", "abstract_method_signature",
    "method_declaration", "type_spec", "function_item", "function_signature_item",
    "struct_item", "enum_item", "trait_item", "impl_item", "record_declaration",
    "constructor_declaration",
}  # fmt: skip
_FUNCTION_LIKE = {
    "function_declaration", "generator_function_declaration", "method_definition",
    "method_declaration", "function_item", "constructor_declaration", "arrow_function",
    "function_expression", "func_literal",
}  # fmt: skip
_REGEX_SIG = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub\s+)?"
                        r"(?:function|class|interface|func|fn|impl)\b|^\s*public .*\(")  # fmt: skip


def _python_signatures(source: str) -> list[str] | None:
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = []
    doc = ast.get_docstring(tree)
    if doc:
        lines.append(f"  doc: {doc.splitlines()[0][:120]}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            lines.append(f"  class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = ", ".join(a.arg for a in node.args.args)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            lines.append(f"  {prefix} {node.name}({args})")
    return lines


def _head(src: bytes, node) -> str:
    """Declaration text up to its body, whitespace collapsed, capped."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte : end].decode(errors="replace")
    return " ".join(text.split()).rstrip("{ ").strip()[:200]


def _tree_sitter_signatures(source: str, lang: str) -> list[str]:
    from tree_sitter_language_pack import get_parser

    src = source.encode()
    root = get_parser(lang).parse(src).root_node
    lines: list[str] = []

    def visit(node, depth: int) -> None:
        for child in node.children:
            kind = child.type
            if kind in _DEFS:
                lines.append("  " * (depth + 1) + _head(src, child))
                if kind not in _FUNCTION_LIKE:
                    visit(child, depth + 1)  # class/interface/impl members
            elif kind == "variable_declarator":
                value = child.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    name = src[child.start_byte : value.start_byte].decode(errors="replace")
                    lines.append("  " * (depth + 1) + " ".join((name + _head(src, value)).split()))
            elif kind not in _FUNCTION_LIKE:
                visit(child, depth)

    visit(root, 0)
    return lines


def _regex_signatures(source: str) -> list[str]:
    return ["  " + " ".join(line.split()).rstrip("{ ")[:200]
            for line in source.splitlines() if _REGEX_SIG.match(line)]  # fmt: skip


def extract_signatures(source: str, filename: str) -> str:
    """Files + API signatures only (T2.4, L2.4) — never snippet bodies.

    `.py` via `ast`; TS/TSX/JS/MJS/Go/Rust/Java via tree-sitter-language-pack
    (ships with aider; imported lazily) with a regex fallback, noted in the
    record, when it is not installed. Empty string = nothing to index.
    """
    suffix = pathlib.PurePath(filename).suffix
    header = [f"source: {filename}", f"file: {filename}"]
    if suffix == ".py":
        body = _python_signatures(source)
        if body is None:
            return ""
    elif suffix in TS_LANG:
        try:
            body = _tree_sitter_signatures(source, TS_LANG[suffix])
        except ImportError:
            body = ["  note: regex fallback (tree-sitter-language-pack not installed)"]
            body += _regex_signatures(source)
    else:
        return ""
    return "\n".join(header + body)


def _indexable(root: pathlib.Path, rel: str) -> bool:
    path = root / rel
    parts = pathlib.PurePosixPath(rel).parts
    return (
        path.is_file()
        and path.suffix in INDEX_SUFFIXES
        and not any(part in SKIP_DIRS for part in parts)
    )


def collect_records(root: pathlib.Path) -> dict[str, str]:
    """{relative source path: codex-index record text} for every indexable file."""
    records = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.suffix not in INDEX_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        sig = extract_signatures(path.read_text(errors="replace"), rel.as_posix())
        if sig:
            records[rel.as_posix()] = f"[codex-index] {sig}"
    return records


async def index_repo(pool: asyncpg.Pool, repo_path: str) -> dict[str, int]:
    """Incrementally index a repo's files + signatures (tag='codex-index').

    One row per source path: unchanged records (same content_sha) are
    skipped, changed ones replaced in place, rows whose source vanished
    deleted — as are legacy pre-L2.4 rows without a source.
    """
    root = pathlib.Path(repo_path)
    records = collect_records(root)
    known = {
        r["source"]: r["content_sha"]
        for r in await pool.fetch(
            "SELECT source, content_sha FROM lessons "
            "WHERE tag = 'codex-index' AND source IS NOT NULL"
        )
    }
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    for source, text in records.items():
        sha = hashlib.sha256(text.encode()).hexdigest()
        if known.get(source) == sha:
            counts["unchanged"] += 1
            continue
        vec = embed(text)
        await pool.execute(
            "INSERT INTO lessons(lesson_id, text, tag, embedding, source, content_sha, embedder) "
            "VALUES($1, $2, 'codex-index', $3, $4, $5, $6) "
            "ON CONFLICT (source) WHERE tag = 'codex-index' DO UPDATE "
            "SET text = EXCLUDED.text, embedding = EXCLUDED.embedding, "
            "embedder = EXCLUDED.embedder, "
            "content_sha = EXCLUDED.content_sha, created_at = now()",
            uuid.uuid4(),
            text,
            to_pgvector(vec),
            source,
            sha,
            stored_embedder(vec),
        )
        counts["updated" if source in known else "inserted"] += 1
    # Only sources that are gone (or no longer indexable) are pruned; a file
    # that exists but fails to parse keeps its last good row.
    gone = [s for s in known if s not in records and not _indexable(root, s)]
    deleted = await pool.fetch(
        "DELETE FROM lessons WHERE tag = 'codex-index' "
        "AND (source IS NULL OR source = ANY($1::text[])) RETURNING lesson_id",
        gone,
    )
    counts["deleted"] = len(deleted)
    return counts


async def reembed(pool: asyncpg.Pool) -> int:
    if embed("probe") is None:  # lessons.py degrades to text-only; a re-embed must not
        raise SystemExit(
            "re-embed refused: the embedding model is unavailable (see the log above); "
            "existing embeddings were left untouched"
        )
    rows = await pool.fetch("SELECT lesson_id, text FROM lessons")
    done = 0
    for r in rows:
        vec = embed(r["text"])
        if vec is None:  # a single failed call must not null a stored vector either
            continue
        await pool.execute(
            "UPDATE lessons SET embedding = $2, embedder = $3 WHERE lesson_id = $1",
            r["lesson_id"],
            to_pgvector(vec),
            embedder_id(),
        )
        done += 1
    if done != len(rows):
        raise SystemExit(
            f"re-embedded {done} of {len(rows)} lesson(s) as {embedder_id()}; the embedder failed "
            "for the rest (see the log) — they were left untouched. Run --reembed again."
        )
    return done


async def run(args: argparse.Namespace) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    if args.query:
        for i, text in enumerate(await search_lessons(pool, args.query), 1):
            print(f"--- {i} ---\n{text[:400]}\n")
    elif args.reembed:
        print(f"re-embedded {await reembed(pool)} lesson(s) as {embedder_id()}")
    elif args.index_repo:
        c = await index_repo(pool, args.index_repo)
        print(
            f"codex-index: {c['inserted']} new, {c['updated']} replaced, "
            f"{c['unchanged']} unchanged, {c['deleted']} deleted (tag='codex-index')"
        )
    else:
        print(f"seeded {await seed(pool)} new lesson chunk(s) from {KNOWLEDGE_DIR}")
    await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reembed", action="store_true")
    p.add_argument("--query", default="")
    p.add_argument("--index-repo", default="", help="repo path to signature-index")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
