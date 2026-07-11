"""Remote messaging -> tasks (runbook §11B, Appendix B).

Telegram is the baseline; Slack/Discord follow the same pattern. Inbound
messages become tasks the same way as issue sync: a tasks row plus an
outbox row in ONE transaction (default role `triage`). The bot token is
leased from OpenBao; this process runs OUTSIDE the agent sandbox but is
still not an agent — it never talks to JetStream directly.

Usage:  python -m ingest.chat_ingress
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid

import asyncpg
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))

from swarm.v1 import task_pb2  # noqa: E402

log = logging.getLogger("chat-ingress")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
DEFAULT_BUDGET = int(os.environ.get("DEFAULT_BUDGET_TOKENS", "200000"))
POLL_S = int(os.environ.get("TELEGRAM_POLL_S", "5"))
ALLOWED_CHAT_IDS = {
    int(c) for c in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if c
}


async def create_task(pool: asyncpg.Pool, ext_id: str, title: str) -> str | None:
    """tasks row + outbox row in one tx; dedupe on ext_id."""
    role = "triage"
    async with pool.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM tasks WHERE ext_id=$1", ext_id):
                return None
            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO tasks(task_id, role, status, ext_id, title, branch, "
                "budget_tokens) VALUES($1, $2, 'pending', $3, $4, $5, $6)",
                uuid.UUID(tid),
                role,
                ext_id,
                title,
                f"feature/{tid}",
                DEFAULT_BUDGET,
            )
            task = task_pb2.Task(
                task_id=tid,
                role=role,
                title=title,
                branch=f"feature/{tid}",
                budget_tokens=DEFAULT_BUDGET,
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                f"swarm.tasks.{role}",
                tid,
                task.SerializeToString(),
            )
    return tid


async def telegram_loop() -> None:
    from agent.openbao import lease_secret

    token = lease_secret("telegram")
    api = f"https://api.telegram.org/bot{token}"
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    offset = 0
    log.info("telegram ingress polling every %ss", POLL_S)
    while True:
        try:
            resp = requests.get(
                f"{api}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            resp.raise_for_status()
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or (ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS):
                    continue
                ext_id = f"telegram#{upd['update_id']}"
                tid = await create_task(pool, ext_id, text)
                if tid:
                    requests.get(
                        f"{api}/sendMessage",
                        params={"chat_id": chat_id, "text": f"task created: {tid}"},
                        timeout=10,
                    )
                    log.info("created triage task %s from %s", tid, ext_id)
        except requests.RequestException as err:
            log.warning("telegram poll failed, retrying: %s", err)
            time.sleep(POLL_S)


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    asyncio.run(telegram_loop())


if __name__ == "__main__":
    main()
