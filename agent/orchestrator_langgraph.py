"""Optional orchestration upgrade: LangGraph (MIT) — runbook §18.

Adopt when you need durable graph state, checkpointing, and human-in-the-
loop pauses instead of the bespoke loop in agent_worker.py. The bus,
outbox, and relay are unchanged: this is a drop-in alternative *consumer*
that runs each task through a checkpointed claim -> work -> emit graph.

    pip install -r requirements-orchestration.txt
    HIL=1 python -m agent.orchestrator_langgraph   # pause before push

With HIL=1 the graph interrupts before the `work` node; resume by rerunning
with the same task (the checkpointer replays completed nodes).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import TypedDict

import asyncpg
import nats
from nats.errors import TimeoutError as NatsTimeoutError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))

from swarm.v1 import event_pb2, task_pb2  # noqa: E402

from agent.agent_worker import ROLE, emit, pg_claim  # noqa: E402
from agent.budget import ValkeyBudget, valkey_lock  # noqa: E402
from agent.openbao import lease_secret  # noqa: E402
from agent.sandbox import run_sandboxed_aider  # noqa: E402

log = logging.getLogger("langgraph-orchestrator")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
HIL = os.environ.get("HIL", "") == "1"  # human-in-the-loop pause before work


class TaskState(TypedDict, total=False):
    task_bytes: bytes
    claimed: bool
    done: bool
    error: str


def build_graph(pool: asyncpg.Pool):
    try:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
    except ImportError as err:
        raise SystemExit(
            "LangGraph is an optional upgrade: "
            "pip install -r requirements-orchestration.txt"
        ) from err

    async def claim(state: TaskState) -> TaskState:
        task = task_pb2.Task.FromString(state["task_bytes"])
        return {"claimed": await pg_claim(pool, task.task_id)}

    async def work(state: TaskState) -> TaskState:
        task = task_pb2.Task.FromString(state["task_bytes"])
        budget = ValkeyBudget(ROLE, task.task_id, task.budget_tokens)
        ttl = (
            max(60, int(task.deadline_unix - time.time()))
            if task.deadline_unix
            else 3600
        )
        async with valkey_lock(f"task:{task.task_id}", ttl=ttl):
            secret = lease_secret("forgejo")
            await run_sandboxed_aider(task, secret, budget=budget)
        return {"done": True}

    async def report(state: TaskState) -> TaskState:
        task = task_pb2.Task.FromString(state["task_bytes"])
        if state.get("done"):
            await emit(
                pool, task.task_id, event_pb2.AgentEvent.PR_OPENED, status="done"
            )
        return state

    g = StateGraph(TaskState)
    g.add_node("claim", claim)
    g.add_node("work", work)
    g.add_node("report", report)
    g.add_edge(START, "claim")
    g.add_conditional_edges(
        "claim", lambda s: "work" if s.get("claimed") else END, ["work", END]
    )
    g.add_edge("work", "report")
    g.add_edge("report", END)
    return g.compile(
        checkpointer=MemorySaver(),
        interrupt_before=["work"] if HIL else [],
    )


async def main() -> None:
    logging.basicConfig(level="INFO")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    graph = build_graph(pool)

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    sub = await js.pull_subscribe(f"swarm.tasks.{ROLE}", durable=ROLE)
    log.info("langgraph orchestrator up (role=%s, HIL=%s)", ROLE, HIL)

    while True:
        try:
            msgs = await sub.fetch(1, timeout=30)
        except NatsTimeoutError:
            continue
        for m in msgs:
            task = task_pb2.Task.FromString(m.data)
            config = {"configurable": {"thread_id": task.task_id}}
            try:
                result = await graph.ainvoke({"task_bytes": m.data}, config)
                if HIL and not result.get("done"):
                    log.info(
                        "task %s paused before work (HIL); resume to continue",
                        task.task_id,
                    )
                    input("press enter to approve and continue...")
                    result = await graph.ainvoke(None, config)  # resume
                await m.ack()
                log.info("task %s -> %s", task.task_id, result)
            except Exception as err:  # graph errors follow worker semantics
                log.error("task %s failed: %s", task.task_id, err)
                await m.nak(delay=30)


if __name__ == "__main__":
    asyncio.run(main())
