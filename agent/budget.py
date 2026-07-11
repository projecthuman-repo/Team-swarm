"""Valkey-backed coordination: distributed task locks and per-agent budgets.

Valkey (BSD-3) replaces Redis for cache/coordination (license gate).
Budgets are Hard-Rule enforcement: an agent that exceeds its token budget
raises BudgetExceeded, the task naks for retry, and repeated overruns are a
pause-autonomy signal (runbook §17).
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import valkey.asyncio as valkey

VALKEY_URL = os.environ.get("VALKEY_URL", "valkey://localhost:6379")

_client: valkey.Valkey | None = None


def client() -> valkey.Valkey:
    global _client
    if _client is None:
        _client = valkey.Valkey.from_url(VALKEY_URL)
    return _client


class BudgetExceeded(Exception):
    """Raised when a task's token spend crosses its budget."""


class ValkeyBudget:
    """Token budget counter for one task, enforced in Valkey.

    Synchronous `spend()` is intentionally simple so the sandboxed aider
    runner (a subprocess wrapper) can charge tokens without an event loop.
    """

    def __init__(self, agent: str, task_id: str, limit: int) -> None:
        self.agent = agent
        self.task_id = task_id
        self.limit = int(limit) if limit else 0
        self.key = f"budget:{agent}:{task_id}"

    async def spend(self, tokens: int) -> None:
        total = await client().incrby(self.key, tokens)
        await client().expire(self.key, 24 * 3600)
        if self.limit and total > self.limit:
            raise BudgetExceeded(
                f"{self.key} spent {total} > budget {self.limit} tokens"
            )

    async def spent(self) -> int:
        val = await client().get(self.key)
        return int(val) if val else 0


@asynccontextmanager
async def valkey_lock(name: str, ttl: int = 3600):
    """SET NX EX distributed lock; released only by its owner."""
    token = uuid.uuid4().hex
    key = f"lock:{name}"
    acquired = await client().set(key, token, nx=True, ex=ttl)
    if not acquired:
        raise TimeoutError(f"lock {key} already held")
    try:
        yield
    finally:
        # Delete only if we still own it (compare-and-delete).
        release = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        await client().eval(release, 1, key, token)
