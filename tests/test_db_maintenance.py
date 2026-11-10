"""L2.5 — storage hygiene: outbox retention + VACUUM on an autocommit connection."""

import importlib.util
import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "db_maintenance", os.path.join(ROOT, "scripts", "db_maintenance.py")
)
dbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dbm)


class FakeConn:
    def __init__(self, in_tx=False, deleted=3):
        self.in_tx, self.deleted, self.log = in_tx, deleted, []

    def is_in_transaction(self):
        return self.in_tx

    async def fetchval(self, sql, *args):
        self.log.append((sql, args, self.in_tx))
        return self.deleted

    async def execute(self, sql, *args):
        self.log.append((sql, args, self.in_tx))


async def test_statements_and_autocommit(capsys):
    conn = FakeConn()
    assert await dbm.maintain(conn, days=7) == 3
    sqls = [entry[0] for entry in conn.log]
    assert "DELETE FROM outbox WHERE published_at IS NOT NULL" in sqls[0]
    assert "published_at < now() - make_interval(days => $1)" in sqls[0]
    assert conn.log[0][1] == (7,)
    assert sqls[1:] == ["VACUUM (ANALYZE) outbox", "VACUUM (ANALYZE) lessons",
                        "VACUUM (ANALYZE) tasks"]  # fmt: skip
    assert all(in_tx is False for _, _, in_tx in conn.log)  # autocommit throughout
    assert "deleted 3 published row(s) older than 7 day(s)" in capsys.readouterr().out


async def test_refuses_a_transaction():
    conn = FakeConn(in_tx=True)
    with pytest.raises(RuntimeError, match="autocommit"):
        await dbm.maintain(conn)
    assert conn.log == []


def test_morning_routine_step_5_and_env():
    routine = open(os.path.join(ROOT, "scripts", "morning_routine.sh")).read()
    assert "=== 5/5 storage hygiene" in routine
    assert "python scripts/db_maintenance.py" in routine
    assert "OUTBOX_RETENTION_DAYS=7" in open(os.path.join(ROOT, ".env.example")).read()


async def test_retention_is_idempotent_in_real_postgres(pg):
    rows = [("old-pub", "now() - interval '8 days'"), ("new-pub", "now() - interval '1 day'"),
            ("pending", "NULL")]  # fmt: skip
    for msg_id, published in rows:
        await pg.execute(
            f"INSERT INTO outbox(subject, msg_id, payload, published_at) "
            f"VALUES('swarm.tasks.backend', $1, '\\x00', {published})",
            msg_id,
        )
    assert await dbm.maintain(pg, days=7) == 1
    assert await dbm.maintain(pg, days=7) == 0  # idempotent
    left = {r["msg_id"] for r in await pg.fetch("SELECT msg_id FROM outbox")}
    assert left == {"new-pub", "pending"}
