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


@pytest.fixture(autouse=True)
def opt_in_flags_off(monkeypatch):
    """Every test starts with the opt-in v4.2 flags off, whatever the caller's
    environment says (review of R11: `ROUTE_API_FALLBACK=1 make
    verify-integration` — the only way to run the real escalation step —
    must not change what the unit suite tests)."""
    for name in ("ROUTE_API_FALLBACK", "RUBRIC_ENABLED", "CODEX_REVIEW", "CODEX_REVIEW_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    try:
        from agent import escalation_claude, review_gate
    except ImportError:  # an environment without the agent's dependencies
        return
    monkeypatch.setattr(escalation_claude, "ROUTE_API_FALLBACK", False)
    for name in ("RUBRIC_ENABLED", "CODEX_REVIEW", "CODEX_REVIEW_REQUIRED"):
        monkeypatch.setattr(review_gate, name, False)


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
