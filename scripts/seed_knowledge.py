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
import os
import pathlib
import sys
import uuid

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.lessons import embed, search_lessons, to_pgvector  # noqa: E402

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
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
            exists = await pool.fetchval(
                "SELECT 1 FROM lessons WHERE text = $1", text
            )
            if exists:
                continue
            await pool.execute(
                "INSERT INTO lessons(lesson_id, text, embedding) VALUES($1, $2, $3)",
                uuid.uuid4(),
                text,
                to_pgvector(embed(text)),
            )
            n += 1
    return n


def extract_signatures(source: str, filename: str) -> str:
    """Files + API signatures only (T2.4) — never snippet bodies."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = [f"file: {filename}"]
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
    return "\n".join(lines) if len(lines) > 1 else lines[0]


async def index_repo(pool: asyncpg.Pool, repo_path: str) -> int:
    """Index a repo's files + signatures into lessons(tag='codex-index')."""
    root = pathlib.Path(repo_path)
    n = 0
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", "node_modules", ".venv", "gen"} for part in path.parts):
            continue
        sig = extract_signatures(path.read_text(errors="replace"), str(path.relative_to(root)))
        if not sig:
            continue
        text = f"[codex-index] {sig}"
        if await pool.fetchval("SELECT 1 FROM lessons WHERE text = $1", text):
            continue
        await pool.execute(
            "INSERT INTO lessons(lesson_id, text, tag, embedding) "
            "VALUES($1, $2, 'codex-index', $3)",
            uuid.uuid4(),
            text,
            to_pgvector(embed(text)),
        )
        n += 1
    return n


async def reembed(pool: asyncpg.Pool) -> int:
    rows = await pool.fetch("SELECT lesson_id, text FROM lessons")
    for r in rows:
        await pool.execute(
            "UPDATE lessons SET embedding = $2 WHERE lesson_id = $1",
            r["lesson_id"],
            to_pgvector(embed(r["text"])),
        )
    return len(rows)


async def run(args: argparse.Namespace) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    if args.query:
        for i, text in enumerate(await search_lessons(pool, args.query), 1):
            print(f"--- {i} ---\n{text[:400]}\n")
    elif args.reembed:
        print(f"re-embedded {await reembed(pool)} lesson(s)")
    elif args.index_repo:
        n = await index_repo(pool, args.index_repo)
        print(f"indexed {n} file signature record(s) (tag='codex-index')")
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
