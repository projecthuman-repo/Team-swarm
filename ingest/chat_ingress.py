"""Remote messaging -> tasks (runbook §11B, Appendix B).

Telegram is the baseline; Slack and Discord are the optional additional
ingresses on the same pattern: inbound messages become tasks — a tasks row
plus an outbox row in ONE transaction (default role `triage`). Bot tokens
are leased from OpenBao (secrets `telegram`, `slack`, `discord`); this
process runs OUTSIDE the agent sandbox but never talks to JetStream.

Select sources with CHAT_SOURCES (comma-separated; default `telegram`):

    CHAT_SOURCES=telegram,slack,discord python -m ingest.chat_ingress

Slack needs SLACK_CHANNEL_ID (bot must be in the channel; reads via
conversations.history). Discord needs DISCORD_CHANNEL_ID (bot needs the
Read Message History permission). Dedupe keys: telegram#<update_id>,
slack#<channel>#<ts>, discord#<message_id>.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
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
POLL_S = int(os.environ.get("CHAT_POLL_S", "5"))
SOURCES = [s.strip() for s in os.environ.get("CHAT_SOURCES", "telegram").split(",") if s.strip()]
TELEGRAM_ALLOWED_CHATS = {
    int(c) for c in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if c
}
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")


async def create_task(pool: asyncpg.Pool, ext_id: str, title: str) -> str | None:
    """tasks row + outbox row in one tx; dedupe on ext_id (idempotent)."""
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


def _get(url: str, **kw) -> dict:
    resp = requests.get(url, timeout=40, **kw)
    resp.raise_for_status()
    return resp.json()


async def telegram_loop(pool: asyncpg.Pool) -> None:
    from agent.openbao import lease_secret

    token = lease_secret("telegram")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    log.info("telegram ingress started")
    while True:
        try:
            data = await asyncio.to_thread(
                _get, f"{api}/getUpdates", params={"offset": offset, "timeout": 30}
            )
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or (
                    TELEGRAM_ALLOWED_CHATS and chat_id not in TELEGRAM_ALLOWED_CHATS
                ):
                    continue
                tid = await create_task(pool, f"telegram#{upd['update_id']}", text)
                if tid:
                    requests.get(
                        f"{api}/sendMessage",
                        params={"chat_id": chat_id, "text": f"task created: {tid}"},
                        timeout=10,
                    )
                    log.info("telegram -> task %s", tid)
        except requests.RequestException as err:
            log.warning("telegram poll failed, retrying: %s", err)
            await asyncio.sleep(POLL_S)


async def slack_loop(pool: asyncpg.Pool) -> None:
    from agent.openbao import lease_secret

    token = lease_secret("slack")
    headers = {"Authorization": f"Bearer {token}"}
    oldest = "0"
    log.info("slack ingress started (channel %s)", SLACK_CHANNEL_ID)
    while True:
        try:
            data = await asyncio.to_thread(
                _get,
                "https://slack.com/api/conversations.history",
                params={"channel": SLACK_CHANNEL_ID, "oldest": oldest, "limit": 50},
                headers=headers,
            )
            for msg in sorted(data.get("messages", []), key=lambda m: m["ts"]):
                oldest = max(oldest, msg["ts"])
                if msg.get("bot_id") or msg.get("subtype"):
                    continue  # ignore bots (incl. ourselves) and joins/etc.
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                ext_id = f"slack#{SLACK_CHANNEL_ID}#{msg['ts']}"
                tid = await create_task(pool, ext_id, text)
                if tid:
                    requests.post(
                        "https://slack.com/api/chat.postMessage",
                        json={
                            "channel": SLACK_CHANNEL_ID,
                            "thread_ts": msg["ts"],
                            "text": f"task created: {tid}",
                        },
                        headers=headers,
                        timeout=10,
                    )
                    log.info("slack -> task %s", tid)
        except requests.RequestException as err:
            log.warning("slack poll failed, retrying: %s", err)
        await asyncio.sleep(POLL_S)


async def discord_loop(pool: asyncpg.Pool) -> None:
    from agent.openbao import lease_secret

    token = lease_secret("discord")
    headers = {"Authorization": f"Bot {token}"}
    api = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    last_id = "0"
    log.info("discord ingress started (channel %s)", DISCORD_CHANNEL_ID)
    while True:
        try:
            msgs = await asyncio.to_thread(
                _get, api, params={"after": last_id, "limit": 50}, headers=headers
            )
            for msg in sorted(msgs, key=lambda m: int(m["id"])):
                last_id = msg["id"]
                if msg.get("author", {}).get("bot"):
                    continue
                text = (msg.get("content") or "").strip()
                if not text:
                    continue
                tid = await create_task(pool, f"discord#{msg['id']}", text)
                if tid:
                    requests.post(
                        api,
                        json={
                            "content": f"task created: {tid}",
                            "message_reference": {"message_id": msg["id"]},
                        },
                        headers=headers,
                        timeout=10,
                    )
                    log.info("discord -> task %s", tid)
        except requests.RequestException as err:
            log.warning("discord poll failed, retrying: %s", err)
        await asyncio.sleep(POLL_S)


LOOPS = {"telegram": telegram_loop, "slack": slack_loop, "discord": discord_loop}


async def main_async() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    unknown = [s for s in SOURCES if s not in LOOPS]
    if unknown:
        raise SystemExit(f"unknown CHAT_SOURCES entries: {unknown}")
    await asyncio.gather(*(LOOPS[s](pool) for s in SOURCES))


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
