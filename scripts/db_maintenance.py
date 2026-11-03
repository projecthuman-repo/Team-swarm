"""Storage hygiene for the Morning Operator Routine (Lean & Precise L2.5).

1. Delete outbox rows the relay published more than OUTBOX_RETENTION_DAYS
   ago (default 7). Unpublished rows are never touched — the outbox is the
   only path into JetStream (Hard Rule 2).
2. VACUUM (ANALYZE) outbox, lessons and tasks — on an autocommit
   connection: VACUUM cannot run inside a transaction block.

Idempotent: a second run deletes nothing and vacuums again.

Usage:  python scripts/db_maintenance.py
"""

from __future__ import annotations

import asyncio
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm")
OUTBOX_RETENTION_DAYS = int(os.environ.get("OUTBOX_RETENTION_DAYS", "7"))
VACUUM_TABLES = ("outbox", "lessons", "tasks")

DELETE_PUBLISHED = (
    "WITH gone AS (DELETE FROM outbox WHERE published_at IS NOT NULL "
    "AND published_at < now() - make_interval(days => $1) RETURNING 1) "
    "SELECT count(*) FROM gone"
)


async def maintain(conn, days: int = OUTBOX_RETENTION_DAYS) -> int:
    """Prune published outbox rows, then vacuum. Returns rows deleted."""
    if conn.is_in_transaction():
        raise RuntimeError("db_maintenance needs an autocommit connection (VACUUM)")
    deleted = await conn.fetchval(DELETE_PUBLISHED, days)
    print(f"outbox: deleted {deleted} published row(s) older than {days} day(s)")
    for table in VACUUM_TABLES:
        await conn.execute(f"VACUUM (ANALYZE) {table}")
    print(f"vacuum (analyze): {', '.join(VACUUM_TABLES)}")
    return deleted


async def main() -> None:
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)  # autocommit outside transaction()
    try:
        await maintain(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
