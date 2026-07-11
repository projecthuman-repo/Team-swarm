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


class SnowballError(Exception):
    """v4.1 T5.3 — expensive-failure circuit breaker tripped.

    A task exceeded its wall-clock or cumulative-attempt budget. The
    worker term()s the message with reason=snowball -> DLQ, countering
    the documented token-snowball failure mode (retries quietly burning
    the budget on an unsolvable task).
    """


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


MAX_TASK_WALL_S = int(os.environ.get("MAX_TASK_WALL_S", "14400"))  # 4h
MAX_TASK_ATTEMPTS = int(os.environ.get("MAX_TASK_ATTEMPTS", "8"))


class CircuitBreaker:
    """Wall-clock + cumulative-attempt caps for one task (v4.1 T5.3).

    Attempts accumulate in Valkey across redeliveries and worker
    restarts, so a task bouncing between instances still trips the
    breaker. Wall clock is measured from the task's created_at.
    """

    def __init__(
        self,
        task_id: str,
        created_at_unix: float,
        max_wall_s: int = MAX_TASK_WALL_S,
        max_attempts: int = MAX_TASK_ATTEMPTS,
    ) -> None:
        self.task_id = task_id
        self.created_at_unix = created_at_unix
        self.max_wall_s = max_wall_s
        self.max_attempts = max_attempts
        self.key = f"attempts:{task_id}"

    def wall_exceeded(self, now_unix: float) -> bool:
        return (now_unix - self.created_at_unix) > self.max_wall_s

    async def record_attempt(self) -> int:
        n = await client().incrby(self.key, 1)
        await client().expire(self.key, 7 * 24 * 3600)
        return int(n)

    async def check(self, now_unix: float) -> None:
        """Raise SnowballError if any cap is exceeded."""
        attempts = await self.record_attempt()
        if attempts > self.max_attempts:
            raise SnowballError(
                f"task {self.task_id}: {attempts} cumulative attempts "
                f"> cap {self.max_attempts}"
            )
        if self.wall_exceeded(now_unix):
            raise SnowballError(
                f"task {self.task_id}: wall clock exceeded {self.max_wall_s}s"
            )


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
