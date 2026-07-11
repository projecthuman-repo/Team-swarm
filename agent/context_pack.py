"""Bounded retrieval context for the engine (v4.1 T2.4).

Pulls repo signatures (lessons tag='codex-index') and role lessons
(tag=<role>) relevant to the task, under a HARD token cap — context rot
means reliability degrades as input grows even within a 256K window
(conventions/context.md). "Similar-snippet" retrieval is deliberately
excluded (degrades results up to 15%).

Enabled with CONTEXT_PACK=1; the worker appends the pack to the task
message so the engine sees files + signatures, never stale code bodies.
"""

from __future__ import annotations

import logging
import os

import asyncpg

from agent.lessons import search_lessons

log = logging.getLogger("context-pack")

CONTEXT_PACK_TOKENS = int(os.environ.get("CONTEXT_PACK_TOKENS", "2000"))
_CHARS_PER_TOKEN = 4  # conservative cap arithmetic without a tokenizer


def clamp_to_cap(chunks: list[str], cap_tokens: int) -> list[str]:
    """Hard token cap: include whole chunks until the budget is spent."""
    budget = cap_tokens * _CHARS_PER_TOKEN
    out: list[str] = []
    for chunk in chunks:
        if len(chunk) > budget:
            break
        out.append(chunk)
        budget -= len(chunk)
    return out


async def build_context(pool: asyncpg.Pool, query: str, role: str) -> str:
    """Retrieval pack: repo signatures + role lessons, capped."""
    signatures = await search_lessons(pool, query, limit=8, tag="codex-index")
    role_lessons = await search_lessons(pool, query, limit=4, tag=role)
    chunks = clamp_to_cap(signatures + role_lessons, CONTEXT_PACK_TOKENS)
    if not chunks:
        return ""
    return (
        "\n\n# Repository context (files + signatures only; hard cap "
        f"{CONTEXT_PACK_TOKENS} tokens)\n" + "\n\n".join(chunks)
    )
