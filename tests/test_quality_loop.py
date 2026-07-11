"""v4.1 T2.1/T2.2/T2.6 — green gate, grounded repair, repro-test policy."""

from agent import sandbox
from agent.sandbox import RunResult, build_message, repair_message


class FakeTask:
    task_id = "t-1"
    title = "Bug: pagination off by one"
    spec_ref = ""
    branch = "feature/t-1"
    verify_cmd = ""
    budget_tokens = 1000
    deadline_unix = 0


def test_repair_message_carries_actual_tool_output():
    msg = repair_message("fix the bug", "FAILED tests/test_x.py::test_y", 1)
    assert "FAILED tests/test_x.py::test_y" in msg
    assert "REPAIR CYCLE 1" in msg
    # external signal only — never ask the model to re-check itself
    assert "re-check yourself" not in msg.lower()


def test_repair_message_truncates_long_output():
    msg = repair_message("fix", "x" * 10_000, 2)
    assert len(msg) < 6_000


def test_build_message_includes_repro_test_policy():
    msg = build_message(FakeTask())
    # conventions/repro-tests.md is appended when the policy is on
    assert "reproduction test" in msg.lower()


def test_build_message_includes_verify_cmd():
    task = FakeTask()
    task.verify_cmd = "pytest tests/test_pagination.py -q"
    msg = build_message(task)
    assert "pytest tests/test_pagination.py -q" in msg


def test_run_result_defaults():
    r = RunResult()
    assert r.first_pass_green is True
    assert r.repair_cycles == 0


def test_max_repair_cycles_default():
    assert sandbox.MAX_REPAIR_CYCLES == 2
