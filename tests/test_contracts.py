"""Contract tests: the checked-in generated code matches the .proto files
and round-trips the fields the swarm depends on."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))

from swarm.v1 import event_pb2, task_pb2


def test_task_round_trip():
    t = task_pb2.Task(
        task_id="11111111-1111-1111-1111-111111111111",
        role="backend",
        title="add healthz",
        branch="feature/11111111-1111-1111-1111-111111111111",
        budget_tokens=200_000,
        deadline_unix=1_800_000_000,
    )
    parsed = task_pb2.Task.FromString(t.SerializeToString())
    assert parsed.role == "backend"
    assert parsed.budget_tokens == 200_000
    assert parsed.branch.startswith("feature/")


def test_event_kinds_match_runbook():
    kinds = event_pb2.AgentEvent.Kind
    assert kinds.CLAIMED == 0
    assert kinds.PROGRESS == 1
    assert kinds.PR_OPENED == 2
    assert kinds.CHECKS_PASSED == 3
    assert kinds.FAILED == 4
    assert kinds.LESSON == 5


def test_event_round_trip():
    e = event_pb2.AgentEvent(
        task_id="t1", agent="review", kind=event_pb2.AgentEvent.LESSON, detail="d"
    )
    parsed = event_pb2.AgentEvent.FromString(e.SerializeToString())
    assert parsed.kind == event_pb2.AgentEvent.LESSON
    assert parsed.agent == "review"
