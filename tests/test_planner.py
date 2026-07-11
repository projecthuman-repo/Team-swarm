"""v4.1 T3.1/T3.2 — verification-aware planning + decomposition policy."""

from agent.planner import DEFAULT_VERIFY_CMD, parse_plan, should_decompose


def test_single_agent_is_default():
    assert should_decompose("add a healthz endpoint") is False


def test_swarm_hard_label_decomposes():
    assert should_decompose("big migration", labels=["swarm-hard"]) is True
    assert should_decompose("[swarm-hard] split the auth module") is True


def test_parse_plan_pairs_every_subtask_with_verification():
    text = """
    1. backend: add the API endpoint :: verify: pytest tests/test_api.py -q
    2. frontend: wire the button :: verify: npm test
    3. review: audit the diff
    """
    subs = parse_plan(text)
    assert len(subs) == 3
    assert subs[0]["verify_cmd"] == "pytest tests/test_api.py -q"
    assert subs[1]["role"] == "frontend"
    # no unverifiable subtasks — missing verify gets the default
    assert subs[2]["verify_cmd"] == DEFAULT_VERIFY_CMD


def test_parse_plan_ignores_non_role_lines():
    assert parse_plan("thinking about it...\nnotes: blah") == []
