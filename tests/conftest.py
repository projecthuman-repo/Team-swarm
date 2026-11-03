"""Shared fixtures.

`pg` — a real Postgres connection inside a throwaway schema with
db/schema.sql applied (pgvector required). Skipped when no Postgres is
reachable at DATABASE_URL, so unit runs stay hermetic; `make
verify-integration` sets REQUIRE_POSTGRES=1, which turns the skip into a
failure — deferred is not skipped.
"""

import os
import uuid

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm")


@pytest.fixture
async def pg():
    asyncpg = pytest.importorskip("asyncpg")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    try:
        admin = await asyncpg.connect(DATABASE_URL, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as err:
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail(f"REQUIRE_POSTGRES=1 but Postgres is not reachable: {err}")
        pytest.skip(f"Postgres not reachable at DATABASE_URL ({err}); runs in verify-integration")
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await admin.execute(f"CREATE SCHEMA {schema}")
    await admin.close()
    conn = await asyncpg.connect(
        DATABASE_URL, timeout=3, server_settings={"search_path": f"{schema}, public"}
    )
    with open(os.path.join(ROOT, "db", "schema.sql")) as f:
        await conn.execute(f.read())
    try:
        yield conn
    finally:
        await conn.execute(f"DROP SCHEMA {schema} CASCADE")
        await conn.close()
