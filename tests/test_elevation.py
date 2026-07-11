"""v4.1 cross-cutting units: circuit breaker, tiers, router, verifier
aggregation, shadow-eval report, headroom SLA lines, RAG indexing."""

import importlib.util
import os

import pytest

from agent.budget import CircuitBreaker, SnowballError
from agent.router import heuristic_tier
from agent.verifier import aggregate, parse_verdict
from ingest.forgejo_issue_sync import subject_for, tier_of


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HERE = os.path.dirname(__file__)
shadow = _load("shadow_eval", os.path.join(HERE, "..", "scripts", "shadow_eval.py"))
sla = _load("sla_report", os.path.join(HERE, "..", "scripts", "sla_report.py"))
seedk = _load("seed_knowledge", os.path.join(HERE, "..", "scripts", "seed_knowledge.py"))
spec_patch = _load(
    "speculative_patch", os.path.join(HERE, "..", "scripts", "speculative_patch.py")
)
learn = _load(
    "morning_headroom_learn",
    os.path.join(HERE, "..", "scripts", "morning_headroom_learn.py"),
)


# ---- T5.3 circuit breaker ---------------------------------------------

def test_wall_clock_cap_trips():
    b = CircuitBreaker("t", created_at_unix=0, max_wall_s=100)
    assert b.wall_exceeded(now_unix=101) is True
    assert b.wall_exceeded(now_unix=50) is False


def test_snowball_is_distinct_from_poison():
    assert issubclass(SnowballError, Exception)


# ---- T4.2 tier subjects ------------------------------------------------

def test_default_tier_preserves_v40_subject():
    assert subject_for("backend", "") == "swarm.tasks.backend"


def test_tier_subject():
    assert subject_for("backend", "35b-q4") == "swarm.tasks.backend.35b-q4"


def test_tier_label_parsing():
    issue = {"labels": [{"name": "swarm-ready"}, {"name": "tier:35b-q4"}]}
    assert tier_of(issue) == "35b-q4"
    assert tier_of({"labels": []}) == ""


# ---- T4.4 router -------------------------------------------------------

def test_router_heuristic_tiers():
    assert heuristic_tier("fix typo in readme") == "9b-q4"
    assert heuristic_tier("refactor the auth architecture") == "35b-q4"


# ---- T2.3 verifier -----------------------------------------------------

def test_verdict_parsing_fail_closed():
    assert parse_verdict("looks fine\nVERDICT: PASS") == "PASS"
    assert parse_verdict("bad\nVERDICT: BLOCK — missing tests") == "BLOCK"
    assert parse_verdict("no explicit verdict") == "BLOCK"


def test_aggregate_any_block_blocks():
    findings = [
        {"lens": "architect", "verdict": "PASS", "notes": ""},
        {"lens": "security", "verdict": "BLOCK", "notes": "injection"},
        {"lens": "qa", "verdict": "PASS", "notes": ""},
    ]
    assert aggregate(findings)["verdict"] == "BLOCK"
    assert aggregate([{"lens": "qa", "verdict": "PASS", "notes": ""}])["verdict"] == "PASS"


def test_aggregate_error_blocks_fail_closed():
    findings = [{"lens": "qa", "verdict": "ERROR", "notes": "endpoint down"}]
    assert aggregate(findings)["verdict"] == "BLOCK"


# ---- T6.1 shadow-eval report -------------------------------------------

def _summary(label, green, tokens):
    return {
        "label": label,
        "tasks": 3,
        "green_rate": green,
        "avg_tokens": tokens,
        "avg_latency_s": 10.0,
        "results": [],
    }


def test_shadow_report_adopts_only_on_win():
    report = shadow.markdown_report(_summary("default", 0.5, 1000),
                                    _summary("cand", 0.8, 900))
    assert "ADOPT" in report
    report = shadow.markdown_report(_summary("default", 0.8, 1000),
                                    _summary("cand", 0.5, 900))
    assert "KEEP default" in report


def test_shadow_report_rejects_token_blowup():
    # better green rate but >10% more tokens is not a clean win
    report = shadow.markdown_report(_summary("default", 0.5, 1000),
                                    _summary("cand", 0.8, 2000))
    assert "KEEP default" in report


# ---- T1.6 measured-vs-estimated savings --------------------------------

def test_headroom_savings_labelled_measured_with_holdout():
    lines = sla.headroom_savings_lines(
        {"tokens_pre": 1000, "tokens_post": 600, "output_tokens_saved": 50},
        holdout=0.1,
    )
    joined = "\n".join(lines)
    assert "40%" in joined
    assert "measured" in joined and "10% unshaped holdout" in joined


def test_headroom_savings_labelled_estimated_without_holdout():
    lines = sla.headroom_savings_lines({"output_tokens_saved": 50}, holdout=0)
    assert "estimated" in "\n".join(lines)


def test_headroom_savings_na_when_proxy_down():
    assert "n/a" in sla.headroom_savings_lines(None, 0.1)[0]


# ---- T2.4 RAG index: signatures only, never bodies ---------------------

def test_extract_signatures_only():
    src = (
        '"""Module doc."""\n\nSECRET = "x"\n\n'
        "def handler(req, resp):\n    return SECRET\n\n"
        "class Store:\n    def get(self, key):\n        return None\n"
    )
    sig = seedk.extract_signatures(src, "app/api.py")
    assert "def handler(req, resp)" in sig
    assert "class Store" in sig
    assert "SECRET" not in sig.replace("file:", "")  # no bodies/constants


def test_clamp_to_cap():
    from agent.context_pack import clamp_to_cap

    chunks = ["a" * 400, "b" * 400, "c" * 400]
    assert clamp_to_cap(chunks, cap_tokens=150) == ["a" * 400]  # 600 chars
    assert clamp_to_cap(chunks, cap_tokens=1000) == chunks


# ---- T2.5 speculative selection ----------------------------------------

def test_select_sole_green_candidate():
    results = [
        {"candidate": 0, "green": False, "tokens": 100},
        {"candidate": 1, "green": True, "tokens": 500},
        {"candidate": 2, "green": False, "tokens": 200},
    ]
    assert spec_patch.select_candidate(results)["candidate"] == 1


def test_select_cheapest_when_multiple_green():
    results = [
        {"candidate": 0, "green": True, "tokens": 900},
        {"candidate": 1, "green": True, "tokens": 300},
    ]
    assert spec_patch.select_candidate(results)["candidate"] == 1


def test_select_none_when_all_red():
    assert spec_patch.select_candidate([{"candidate": 0, "green": False}]) is None


def test_budget_blocks_fanout():
    assert spec_patch.budget_allows_fanout(spent=90_000, limit=100_000, n=3) is False
    assert spec_patch.budget_allows_fanout(spent=1_000, limit=100_000, n=3) is True
    assert spec_patch.budget_allows_fanout(spent=0, limit=0, n=3) is True  # no cap


# ---- T1.5 headroom learn fixture path ----------------------------------

FIXTURE_LOG = """
Analyzing 12 sessions...
Recommendations:
- Always run `pytest -q` before pushing; 3 sessions pushed red.
- The repo uses asyncpg, not psycopg2 — 2 sessions imported the wrong driver.
"""


def test_learn_parses_fixture_log():
    corrections = learn.run_learn(fixture_log=FIXTURE_LOG)
    assert "pytest -q" in corrections
    assert "asyncpg" in corrections


def test_learn_pr_body_never_direct_write():
    body = learn.build_pr_body("- fix things", "2026-07-11")
    assert "NEVER write directly" in body
    assert "headroom-learn" in body


# ---- proto append-only fields (T3.1 AC) ---------------------------------

def test_task_proto_new_fields_are_append_only():
    import sys

    sys.path.insert(0, os.path.join(HERE, "..", "agent", "gen"))
    from swarm.v1 import task_pb2

    t = task_pb2.Task(task_id="x", parent_task_id="p", verify_cmd="make test",
                      tier="9b-q4")
    parsed = task_pb2.Task.FromString(t.SerializeToString())
    assert parsed.parent_task_id == "p"
    assert parsed.verify_cmd == "make test"
    assert parsed.tier == "9b-q4"
    # old readers: fields 9-11 unknown -> still parse fields 1-8
    assert parsed.task_id == "x"


# ---- T6.2 morning audit mapping -----------------------------------------

def test_morning_audit_flags_seeded_bad_merge():
    audit = _load(
        "morning_audit", os.path.join(HERE, "..", "scripts", "morning_audit.py")
    )
    pr = {"number": 7, "title": "sneaky merge"}
    blocked = {
        "verdict": "BLOCK",
        "blocking": [{"lens": "security", "verdict": "BLOCK", "notes": "drops auth check"}],
    }
    lesson, issue = audit.finding_actions(pr, blocked)
    assert "PR #7" in lesson and "drops auth check" in lesson
    assert "needs human review" in issue

    clean = {"verdict": "PASS", "blocking": []}
    assert audit.finding_actions(pr, clean) == (None, None)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
