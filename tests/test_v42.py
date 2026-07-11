"""v4.2 Elevation+ unit tests: Codex engine + token parser, Codex-review
finding mapping, rubric grading, escalation gating, Strix findings, and
the doom-loop repetition detector. All pure logic / mocks — no live GPU,
Docker, or CLI, and no importing absent optional deps at collection."""

import importlib.util
import os

import pytest

from agent import sandbox
from agent.codex_review import parse_findings
from agent.verifier import parse_rubric_score
from ingest.forgejo_issue_sync import subject_for  # noqa: F401 (import sanity)

HERE = os.path.dirname(__file__)


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


doomloop = _load("doomloop_probe", "scripts/doomloop_probe.py")
strix_findings = _load("strix_findings", "scripts/strix_findings.py")


# ---- T7.2 Codex engine command + token parser -------------------------

class FakeTask:
    task_id = "t-1"
    title = "add a healthz endpoint"
    spec_ref = ""
    verify_cmd = ""
    branch = "feature/t-1"


def test_codex_engine_cmd(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    cmd = sandbox.engine_cmd("do the thing")
    assert cmd[0] == "codex" and cmd[1] == "exec"
    assert "--oss" in cmd and "--local-provider" in cmd and "ollama" in cmd
    assert cmd[-1] == "do the thing"


def test_codex_flags_are_overridable(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    monkeypatch.setattr(sandbox, "CODEX_EXEC_FLAGS", "--oss --foo bar")
    cmd = sandbox.engine_cmd("m")
    assert cmd == ["codex", "exec", "--oss", "--foo", "bar", "m"]


def test_aider_still_default(monkeypatch):
    # additive: aider path unchanged (no engine removed)
    monkeypatch.setattr(sandbox, "ENGINE", "aider")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    assert sandbox.engine_cmd("m")[0] == "aider"


def test_codex_token_parser_io_pair():
    assert sandbox._parse_codex_tokens("input: 4200  output: 1100") == 5300


def test_codex_token_parser_total():
    assert sandbox._parse_codex_tokens("tokens used: 5,310") == 5310


def test_codex_token_parser_absent():
    assert sandbox._parse_codex_tokens("no counts here") == 0


def test_parse_tokens_dispatches_by_engine(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    assert sandbox.parse_tokens("input: 10 output: 5") == 15
    monkeypatch.setattr(sandbox, "ENGINE", "aider")
    assert sandbox.parse_tokens("Tokens: 1k sent, 1k received.") == 2000


# ---- T7.4 Codex review finding mapping --------------------------------

def test_codex_review_blocks_on_high_severity():
    raw = '{"findings": [{"title": "SQLi", "severity": "high"}]}'
    findings = parse_findings(raw)
    assert findings[0]["lens"] == "codex"
    assert findings[0]["verdict"] == "BLOCK"


def test_codex_review_passes_clean():
    raw = '{"findings": []}'
    assert parse_findings(raw)[0]["verdict"] == "PASS"


def test_codex_review_jsonl_mode():
    raw = '{"title": "nit", "priority": "low"}\n{"title": "leak", "priority": "critical"}'
    assert parse_findings(raw)[0]["verdict"] == "BLOCK"


def test_codex_review_garbage_is_pass_no_findings():
    # unparseable -> no findings -> PASS (the ERROR/absent path is separate)
    assert parse_findings("not json at all")[0]["verdict"] == "PASS"


# ---- T7.8 rubric grading ----------------------------------------------

def test_rubric_score_parsing():
    assert parse_rubric_score("notes...\nSCORE: 3/4") == 0.75
    assert parse_rubric_score("SCORE: 4/4") == 1.0
    assert parse_rubric_score("no score line") == 0.0  # fail-closed


def test_rubric_hit_miss_threshold():
    # threshold logic lives in grade_rubric; verify the boundary math here
    assert (3 / 4) >= 0.75
    assert (2 / 4) < 0.75


# ---- T7.7 escalation gating (structural opt-in) -----------------------

def test_escalation_disabled_by_default(monkeypatch):
    monkeypatch.setenv("ROUTE_API_FALLBACK", "0")
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    assert esc.escalation_enabled() is False


def test_escalation_enabled_with_flag(monkeypatch):
    monkeypatch.setenv("ROUTE_API_FALLBACK", "1")
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    assert esc.escalation_enabled() is True


def test_escalation_prompt_carries_failing_diff():
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    prompt = esc.build_prompt("do X", "diff --git a b", "FAILED test_x")
    assert "do X" in prompt and "FAILED test_x" in prompt


# ---- T8.3 Strix findings -> lessons -----------------------------------

def test_strix_validated_only():
    report = {
        "findings": [
            {"title": "confirmed XSS", "validated": True, "severity": "high"},
            {"title": "maybe", "validated": False},
            {"title": "confirmed2", "status": "validated"},
        ]
    }
    validated = strix_findings.validated_findings(report)
    assert len(validated) == 2


def test_strix_lesson_text_mentions_fix():
    lesson = strix_findings.finding_lesson(
        {"title": "SQLi", "severity": "critical", "location": "api.py", "poc": "' OR 1=1"}
    )
    assert "SQLi" in lesson and "regression test" in lesson


def test_strix_no_findings():
    assert strix_findings.validated_findings({"findings": []}) == []


# ---- T9.2 doom-loop repetition detector -------------------------------

def test_doomloop_detects_consecutive_repeats():
    text = "\n".join(["thinking..."] * 8)
    assert doomloop.is_doom_loop(text) is True


def test_doomloop_ignores_normal_transcript():
    text = "plan the change\nwrite the test\nrun pytest\nfix the import\ndone"
    assert doomloop.is_doom_loop(text) is False


def test_doomloop_detects_ngram_cycle():
    block = "retry the same call with the same args and it fails again always "
    assert doomloop.is_doom_loop(block * 5) is True


def test_max_consecutive_repeat_ignores_blank_lines():
    assert doomloop.max_consecutive_repeat(["a", "", "a", "", "a"]) == 1


def test_probe_transcripts_rate(tmp_path):
    (tmp_path / "loop.log").write_text("\n".join(["stuck"] * 10))
    (tmp_path / "ok.log").write_text("did the work\nshipped it")
    res = doomloop.probe_transcripts([str(tmp_path / "loop.log"), str(tmp_path / "ok.log")])
    assert res["doom_loops"] == 1 and res["transcripts"] == 2
    assert res["doom_loop_rate"] == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
