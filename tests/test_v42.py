"""v4.2 Elevation+ unit tests: Codex engine + token parser, Codex-review
finding mapping, rubric grading, escalation gating, Strix findings, and
the doom-loop repetition detector. All pure logic / mocks — no live GPU,
Docker, or CLI, and no importing absent optional deps at collection."""

import importlib.util
import json
import os

import pytest

from agent import sandbox
from agent.codex_review import parse_findings
from agent.verifier import parse_rubric_score
from ingest.forgejo_issue_sync import subject_for  # noqa: F401 (import sanity)

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))


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


def test_codex_engine_defaults_select_the_swarms_model_and_can_edit(monkeypatch):
    # review of R7, against the pinned CLI (rust-v0.167.0): `--oss` without -m
    # runs Codex's own gpt-oss:20b (and asks Ollama to pull it); no --sandbox
    # means read-only; and Codex's sandbox cannot start in the hardened container
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    cmd = sandbox.engine_cmd("do the thing")
    assert cmd[cmd.index("-m") + 1] == "maxwell1500/ornith-9b:Q4_K_M"  # as Ollama lists it
    assert sandbox.MODEL == "ollama/maxwell1500/ornith-9b:Q4_K_M"  # aider's spelling of the same
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    assert cmd[cmd.index("--local-provider") + 1] == "ollama"
    assert "--skip-git-repo-check" in cmd and cmd[cmd.index("--color") + 1] == "never"
    assert "--full-auto" not in cmd  # does not exist at this version
    monkeypatch.setenv("CODEX_MODEL", "ornith-35b:q4")
    assert sandbox.codex_model() == "ornith-35b:q4"
    # the same flags in the egress audit, the docs and the example env
    audit = _text("scripts", "codex_egress_audit.sh")
    assert 'MODEL="${CODEX_MODEL:-maxwell1500/ornith-9b:Q4_K_M}"' in audit
    assert 'FLAGS=(--oss --local-provider ollama -m "$MODEL" --sandbox danger-full-access' in audit
    assert audit.count('codex exec "${FLAGS[@]}" "$TASK"') == 2
    assert "Pull a model (make models) and re-run" not in audit  # could not fix a wrong model
    default = "--oss --local-provider ollama -m maxwell1500/ornith-9b:Q4_K_M --sandbox danger-full-access --skip-git-repo-check --color never"  # noqa: E501
    assert f"#CODEX_EXEC_FLAGS={default}" in _text(".env.example")
    assert default.replace(" -m maxwell1500/ornith-9b:Q4_K_M", "") in " ".join(
        sandbox.CODEX_EXEC_FLAGS.replace("-m maxwell1500/ornith-9b:Q4_K_M ", "").split()
    )


def test_codex_token_meter_reads_what_exec_prints():
    # exec's human output at shutdown: "tokens used" then the number, on stderr
    out = "codex\nDone.\ntokens used\n12,345\n"
    assert sandbox._parse_codex_tokens(out) == 12345
    assert sandbox._parse_codex_tokens("no usage reported") == 0


def test_codex_lane_can_start_codex(tmp_path, monkeypatch):
    # a set CODEX_HOME must exist or `codex exec` exits 1; the lane's is a fresh tmpfs
    home = tmp_path / "home" / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(sandbox, "ENGINE", "aider")
    sandbox.prepare_engine()
    assert not home.exists()  # nothing changes for the other engines
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    sandbox.prepare_engine()
    sandbox.prepare_engine()  # idempotent
    assert home.is_dir()
    assert (
        "    prepare_engine()\n"
        in _text("agent", "sandbox.py").split("async def run_sandboxed_aider")[1].split("try:")[0]
    )
    service = yaml.safe_load(_text("deploy", "compose.yaml"))["services"]["codex-engine"]
    env = service["environment"]
    # Codex's `ollama` provider talks to localhost unless this is set
    assert env["CODEX_OSS_BASE_URL"] == "http://ollama:11434/v1"
    assert env["CODEX_HOME"] == "/home/swarm/.codex"
    assert any(t.startswith("/home/swarm:") for t in service["tmpfs"])  # writable, not /tmp
    assert not env["CODEX_HOME"].startswith("/tmp")  # Codex refuses helpers under the temp dir


def test_codex_lane_image_carries_the_offline_profile():
    profile = tomllib.loads(_text("deploy", "codex", "config.toml"))
    assert profile == {
        "check_for_update_on_startup": False,
        "analytics": {"enabled": False},
        "feedback": {"enabled": False},
        "otel": {"metrics_exporter": "none"},
        "features": {"plugins": False},  # else start-up runs git ls-remote on github.com
    }
    image = _text("deploy", "Containerfile.codex")
    assert "COPY deploy/codex/config.toml /etc/codex/config.toml" in image  # the system layer
    # the egress audit checks the same profile
    audit = _text("scripts", "codex_egress_audit.sh")
    written = audit.split("cat > \"$CODEX_HOME_DIR/config.toml\" <<'EOF'\n")[1].split("\nEOF")[0]
    assert tomllib.loads(written) == profile


async def test_engine_subprocesses_do_not_inherit_stdin(tmp_path):
    # `codex exec "<prompt>"` appends piped stdin to the prompt and waits for EOF
    code, out = await sandbox._run(["cat"], str(tmp_path), dict(os.environ), 10)
    assert (code, out) == (0, "")


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


def test_codex_review_garbage_is_an_error_not_a_pass():
    # review R7: this test used to pin the fail-open behaviour ("garbage ->
    # PASS"). A reviewer whose output cannot be read has reviewed nothing.
    for raw in ("not json at all", "", "   \n"):
        assert parse_findings(raw)[0]["verdict"] == "ERROR", repr(raw)
    assert parse_findings("[]")[0]["verdict"] == "PASS"  # an explicit empty list is clean


def _stream(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _message(text):
    item = {"id": "item_1", "type": "agent_message", "text": text}
    return {"type": "item.completed", "item": item}


# `codex exec --json` event shapes, from exec_events.rs at rust-v0.167.0
STARTED = [{"type": "thread.started", "thread_id": "0199a213"}, {"type": "turn.started"}]
USAGE = {"input_tokens": 12034, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
         "output_tokens": 211, "reasoning_output_tokens": 96}  # fmt: skip
COMPLETED = {"type": "turn.completed", "usage": USAGE}
CLEAN_DOC = json.dumps({"findings": [], "overall_correctness": "patch is correct",
                        "overall_explanation": "No blocking issues."})  # fmt: skip


def _doc(priority, correctness="patch is correct"):
    finding = {"title": "Off-by-one in page offset", "body": "skips page 1", "priority": priority,
               "file": "app/pager.py", "line": 42}  # fmt: skip
    return json.dumps({"findings": [finding], "overall_correctness": correctness,
                       "overall_explanation": "x"})  # fmt: skip


@pytest.fixture
def codex(monkeypatch):
    """A fake `codex` process: set .stdout / .returncode, read .calls."""
    import subprocess
    import types

    from agent import codex_review

    fake = types.SimpleNamespace(stdout="", returncode=0, calls=[])
    fake.diff = "diff --git a/app/pager.py b/app/pager.py\n+offset = page * size\n"

    def run(cmd, **kwargs):
        if cmd[0] == "git":
            out = fake.diff if cmd[1] == "diff" else "1a2b3c4\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        fake.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, fake.returncode, stdout=fake.stdout, stderr="boom")

    monkeypatch.setattr(codex_review.subprocess, "run", run)
    monkeypatch.setattr(codex_review, "codex_available", lambda: True)
    monkeypatch.setattr(codex_review, "CODEX_REVIEW_FLAGS", "--oss --local-provider ollama")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    fake.review = lambda: codex_review.review("integration", "/repo")[0]
    return fake


def test_codex_review_nonzero_exit_is_an_error(codex):
    codex.stdout, codex.returncode = _stream(*STARTED, _message(CLEAN_DOC), COMPLETED), 2
    assert codex.review()["verdict"] == "ERROR"


def test_codex_review_invocation_matches_the_pinned_cli(codex):
    # review of R7: `--oss` alone has no provider, no `-m` means Codex's own
    # gpt-oss:20b, and `exec review --base` cannot say PASS. This test used to
    # pin `codex exec --oss --json review --base <branch>` with a stdout no
    # Codex version prints.
    codex.stdout = _stream(*STARTED, _message(CLEAN_DOC), COMPLETED)
    assert codex.review() == {"lens": "codex", "verdict": "PASS", "notes": "No blocking issues."}
    (cmd, kwargs), = codex.calls  # fmt: skip
    assert cmd[:5] == ["codex", "exec", "--oss", "--local-provider", "ollama"]
    assert cmd[5:7] == ["-m", "maxwell1500/ornith-9b:Q4_K_M"]  # the swarm's model, no `ollama/`
    assert cmd[7:12] == ["-s", "read-only", "--skip-git-repo-check", "--ephemeral", "--json"]
    assert cmd[12] == "--output-schema" and cmd[13].endswith("review.schema.json")
    assert "review" not in cmd[:14]  # not the `exec review` subcommand
    # the merge base is resolved here and the diff is IN the prompt: Codex's
    # read-only sandbox cannot run `git diff` in the hardened containers
    assert "base commit 1a2b3c4" in cmd[14] and len(cmd) == 15
    assert "```diff\ndiff --git a/app/pager.py b/app/pager.py\n+offset = page * size" in cmd[14]
    assert kwargs["cwd"] == "/repo" and kwargs["stdin"] is codex_review_subprocess().DEVNULL


def codex_review_subprocess():
    from agent import codex_review

    return codex_review.subprocess


@pytest.mark.parametrize(
    "document, verdict",
    [
        (CLEAN_DOC, "PASS"),
        (_doc(3), "PASS"),  # a nit does not block
        (_doc(2), "PASS"),
        (_doc(1), "BLOCK"),  # P1 and P0 block (CODEX_REVIEW_BLOCK_PRIORITY=1)
        (_doc(0), "BLOCK"),
        (_doc(3, "patch is incorrect"), "BLOCK"),  # the overall verdict blocks on its own
    ],
)
def test_codex_review_verdict_comes_from_the_document(codex, document, verdict):
    codex.stdout = _stream(*STARTED, _message("thinking aloud"), _message(document), COMPLETED)
    found = codex.review()
    assert found["verdict"] == verdict and found["lens"] == "codex"
    if verdict == "BLOCK":
        assert "Off-by-one in page offset (app/pager.py:42)" in found["notes"]


@pytest.mark.parametrize(
    "stream",
    [
        # the model call failed: exit 1 in real life, but the stream alone must be enough
        _stream(*STARTED, {"type": "error", "message": "unexpected status 503"},
                {"type": "turn.failed", "error": {"message": "unexpected status 503"}}),
        _stream(*STARTED, {"type": "error", "message": "stream disconnected"},
                _message(CLEAN_DOC), COMPLETED),
        _stream(*STARTED, _message(CLEAN_DOC)),  # never completed
        _stream(*STARTED, COMPLETED),  # completed without saying anything
        # prose instead of the document — what `exec review` prints for a clean review
        _stream(*STARTED, _message("The change is small and no issues were found."), COMPLETED),
        _stream(*STARTED, _message("Review was interrupted. Please re-run /review and wait "
                                   "for it to complete."), COMPLETED),
        # documents that do not say what PASS needs
        _stream(*STARTED, _message('{"findings": []}'), COMPLETED),
        _stream(*STARTED, _message('{"findings": [], "overall_correctness": "good"}'), COMPLETED),
        _stream(*STARTED, _message(_doc(None)), COMPLETED),
        _stream(*STARTED, _message(_doc("high")), COMPLETED),
        _stream(*STARTED, _message(_doc(7)), COMPLETED),
        _stream(*STARTED, _message("null"), COMPLETED),
        "not json at all\n",
        "",
        '{"findings": []}\n',  # a bare document is not an event stream
    ],
)  # fmt: skip
def test_codex_review_is_an_error_whenever_it_cannot_be_judged(codex, stream):
    codex.stdout = stream
    assert codex.review()["verdict"] == "ERROR"


def test_codex_review_never_judges_a_diff_it_was_not_given_in_full(codex, monkeypatch):
    from agent import codex_review

    codex.stdout = _stream(*STARTED, _message(CLEAN_DOC), COMPLETED)
    codex.diff = ""  # nothing to review is not a clean review
    found = codex.review()
    assert found["verdict"] == "ERROR" and "nothing to review" in found["notes"]
    codex.diff = "+x\n" * 100
    monkeypatch.setattr(codex_review, "MAX_DIFF_CHARS", 50)
    found = codex.review()
    assert found["verdict"] == "ERROR" and "larger than the reviewer is given" in found["notes"]
    assert codex.calls == []  # Codex was never asked


def test_codex_review_diff_covers_new_files_and_leaves_the_index_alone(tmp_path):
    import subprocess

    from agent import codex_review

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")  # fmt: skip

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True,
                              capture_output=True, text=True).stdout  # fmt: skip

    (tmp_path / "app.py").write_text("x = 1\n")
    git("init", "-q", "-b", "integration")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("checkout", "-qb", "feature/t-1")
    (tmp_path / "app.py").write_text("x = 2\n")
    git("commit", "-qam", "committed by the engine")
    (tmp_path / "new.py").write_text("y = 3\n")  # untracked: an engine that does not commit
    sha = codex_review.merge_base("integration", str(tmp_path))
    assert sha == git("rev-parse", "integration").strip()
    diff = codex_review.diff_against(sha, str(tmp_path))
    assert "+x = 2" in diff and "+y = 3" in diff
    assert git("status", "--porcelain") == "?? new.py\n"  # still untracked: index untouched


def test_codex_review_an_unresolvable_base_is_an_error(monkeypatch):
    import subprocess

    from agent import codex_review

    monkeypatch.setattr(codex_review, "codex_available", lambda: True)
    monkeypatch.setattr(
        codex_review.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="bad revision"),
    )
    (found,) = codex_review.review("no-such-branch")
    assert found["verdict"] == "ERROR" and "cannot resolve the review base" in found["notes"]


def test_legacy_findings_parser_no_longer_passes_what_is_not_a_finding_list():
    # review of R7: any parseable JSON without a high priority used to be PASS
    events = _stream(*STARTED, _message("[P0] SQL injection in /login"), COMPLETED)
    failed = _stream(*STARTED, {"type": "turn.failed", "error": {"message": "model not found"}})
    for raw in (events, failed, "null", "42", '"oops"', '{"type": "error", "message": "x"}',
                '{"unexpected": true}', '[{"type": "turn.completed"}]', '[1, 2]'):  # fmt: skip
        assert parse_findings(raw)[0]["verdict"] == "ERROR", raw
    # ...and still reads the documents it was written for
    assert parse_findings('{"findings": []}')[0]["verdict"] == "PASS"
    assert parse_findings('[{"title": "nit", "priority": "low"}]')[0]["verdict"] == "PASS"


def test_codex_review_defaults_are_documented_as_they_are():
    from agent import codex_review

    assert codex_review.CODEX_REVIEW_FLAGS in ("--oss --local-provider ollama",)
    env_example = _text(".env.example")
    assert "#CODEX_REVIEW_FLAGS=--oss --local-provider ollama" in env_example
    assert "#CODEX_REVIEW_BLOCK_PRIORITY=1" in env_example and "#CODEX_MODEL=" in env_example
    assert "exec review --base" in _text("docs", "RUNBOOK.md")  # says why it is not used


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


# ---- review R7: v4.2 as a citizen of the lean toolchain ----------------

import re  # noqa: E402
import tomllib  # noqa: E402

import yaml  # noqa: E402


def _text(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_v42_dependencies_live_in_the_lock():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)
    assert project["project"]["optional-dependencies"]["escalation"] == ["claude-agent-sdk>=0.2"]
    assert project["dependency-groups"]["security"] == ["strix-agent>=1.0.4"]
    conflicts = [sorted(c["group"] for c in pair) for pair in project["tool"]["uv"]["conflicts"]]
    for other in ("agent", "ingest", "headroom-proxy"):
        assert sorted([other, "security"]) in conflicts, other
    for name, needle in (("requirements-security.txt", r"^strix-agent=="),
                         ("requirements-escalation.txt", r"^claude-agent-sdk==")):  # fmt: skip
        text = _text(name)
        assert text.startswith("# GENERATED by make lock"), name
        assert re.search(needle, text, re.M), name
    # the image closures never carry them
    for name in ("requirements.txt", "requirements-ingest.txt", "requirements-headroom-proxy.txt"):
        assert not re.search(r"^(strix-agent|claude-agent-sdk|cvss)==", _text(name), re.M), name
    assert "claude-agent-sdk     -> extra `escalation`" in _text("requirements-elevation.txt")


def test_codex_image_is_built_from_the_lock_with_a_pinned_cli():
    text = _text("deploy", "Containerfile.codex")
    assert "uv sync --frozen --no-dev --no-install-project --group agent --group gate" in text
    assert "pip install" not in text and "curl" not in text and "|| echo" not in text
    assert re.search(r"^ARG CODEX_VERSION=\d+\.\d+\.\d+$", text, re.M)  # no `latest`
    # the npm package is a Node launcher: the runtime must stay in the image
    assert "COPY --from=codex /usr/local/bin/node /usr/local/bin/node" in text
    assert "apt-get purge" not in text and "codex --version" in text
    assert "useradd --create-home --uid 10001 swarm" in text and "AGENT_ENGINE=codex" in text


def test_codex_image_ships_only_the_locked_npm_payload_and_the_gate_scans_it():
    # review of R7: the image copied node:22-slim's whole global node_modules —
    # npm (Artistic-2.0) and corepack in a Zone C image, seen by no gate
    import json

    text = _text("deploy", "Containerfile.codex")
    assert "/usr/local/lib/node_modules" not in text.split("FROM python:3.12-slim\nRUN apt-get")[1]
    assert "npm install -g" not in text
    assert "COPY deploy/codex/package.json deploy/codex/package-lock.json ./" in text
    assert "npm ci --omit=dev --ignore-scripts" in text
    assert "COPY --from=codex /opt/codex/node_modules /opt/codex/node_modules" in text
    # the platform package also carries LGPL binaries beside the Apache-2.0 CLI
    # (a bundled bubblewrap, a GStreamer runtime for voice): removed, and the
    # runtime stage fails the build if anything of the kind is left
    for bundled in ("codex-resources/voice", "codex-resources/bwrap"):
        assert f"node_modules/@openai/codex-linux-*/vendor/*/{bundled}" in text
    assert "-name bwrap -o -name 'libgst*' -o -name 'libglib*' -o -name voice" in text
    assert 'test -z "$(find /opt/codex/node_modules' in text
    lock = json.loads(_text("deploy", "codex", "package-lock.json"))
    manifest = json.loads(_text("deploy", "codex", "package.json"))
    arg = re.search(r"^ARG CODEX_VERSION=(\S+)$", text, re.M).group(1)
    assert manifest["dependencies"] == {"@openai/codex": arg}  # exact, no range
    packages = {k: v for k, v in lock["packages"].items() if k}
    assert packages["node_modules/@openai/codex"]["version"] == arg
    assert len(packages) == 7  # the launcher + six platform packages, nothing else
    for path, package in packages.items():
        assert path.startswith("node_modules/@openai/codex"), path
        assert package["license"] == "Apache-2.0" and package["integrity"].startswith("sha512-")
    spec = importlib.util.spec_from_file_location(
        "license_gate_codex", os.path.join(ROOT, ".forgejo", "scripts", "license_gate.py")
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert "deploy/codex/package-lock.json" in gate.DEFAULT_PATHS["C"]
    assert gate.source_for("deploy/codex/package-lock.json").startswith("file:")
    assert "npm (Artistic-2.0) and corepack" in _text("conventions", "licensing.md")


def test_codex_lane_is_hardened_like_the_agents_but_keeps_its_egress():
    services = yaml.safe_load(_text("deploy", "compose.yaml"))["services"]
    codex = services["codex-engine"]
    assert codex["profiles"] == ["codex"] and codex["read_only"] is True
    assert codex["tmpfs"] == services["agent-backend"]["tmpfs"]
    assert codex["cap_drop"] == ["ALL"]
    assert codex["networks"] == ["swarm-internal", "egress"]  # the controlled-egress lane
    for name in ("agent-backend", "agent-frontend", "agent-review", "agent-triage"):
        assert services[name]["networks"] == ["swarm-internal"]  # sealed agents unchanged


def test_strix_lane_runs_the_cli_from_the_locked_group():
    # review R10: one script behind `make strix`, `make strix-scan` and the workflow
    script = _text("scripts", "strix_quick_scan.sh")
    assert "uv sync -q --frozen --no-install-project --only-group security" in script
    assert '"$ENV_DIR/bin/strix" -n -t "$TARGET" -m "$MODE" ${gate[@]+"${gate[@]}"} "$@"' in script
    for default in ('MODE="${STRIX_SCAN_MODE:-quick}"', 'TARGET="${STRIX_TARGET:-./}"',
                    'STRIX_TELEMETRY="${STRIX_TELEMETRY:-false}"',
                    'LLM_API_BASE="${LLM_API_BASE:-http://localhost:4000/v1}"'):  # fmt: skip
        assert default in script, default
    workflow = _text(".forgejo", "workflows", "security.yaml")
    assert (
        "./scripts/strix_quick_scan.sh --scope-mode diff --diff-base origin/integration" in workflow
    )
    assert "pip install --quiet -r" not in workflow and "--output json" not in workflow
    assert "continue-on-error: true" in workflow  # still advisory, as v4.2 shipped it
    assert not os.path.isdir(os.path.join(ROOT, ".github", "workflows"))  # Forgejo Actions only


FAKE_UV = """#!/bin/sh
# stands in for uv: `sync` installs nothing, `run --no-sync python ...` is recorded
if [ "$1" = sync ]; then exit 0; fi
echo "$@" >> "$FAKE_LOG"
"""
FAKE_STRIX = """#!/bin/sh
echo "strix $@" >> "$FAKE_LOG"
if [ -n "$FAKE_REPORT" ]; then
  mkdir -p strix_runs/run-1
  echo "$FAKE_REPORT" > strix_runs/run-1/vulnerabilities.json
fi
exit "$FAKE_RC"
"""
ONE_FINDING = '[{"id": "v1", "title": "SQL injection in /login", "severity": "high"}]'


def run_strix_lane(tmp_path, rc, report="", stale=False, **env):
    """The lane script in a scratch tree, with a fake uv and a fake strix CLI."""
    import shutil
    import subprocess

    (tmp_path / "scripts").mkdir()
    shutil.copy(os.path.join(ROOT, "scripts", "strix_quick_scan.sh"), tmp_path / "scripts")
    for name, body in (("uv", FAKE_UV), ("strix", FAKE_STRIX)):
        path = tmp_path / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
    if stale:  # a report left by an earlier run
        old = tmp_path / "strix_runs" / "run-0"
        old.mkdir(parents=True)
        (old / "vulnerabilities.json").write_text(ONE_FINDING)
        os.utime(old / "vulnerabilities.json", (1, 1))
    log = tmp_path / "calls.log"
    log.write_text("")
    done = subprocess.run(
        [str(tmp_path / "scripts" / "strix_quick_scan.sh")],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "STRIX_ENV": str(tmp_path),
            "FAKE_LOG": str(log),
            "FAKE_RC": str(rc),
            "FAKE_REPORT": report,
            **env,
        },
    )
    return done, log.read_text()


def test_strix_lane_a_scan_with_findings_is_routed_and_is_not_a_failure(tmp_path):
    # strix-agent 1.7.0 exits 2 when it has findings; that is a completed scan
    done, calls = run_strix_lane(tmp_path, rc=2, report=ONE_FINDING)
    assert done.returncode == 0, done.stderr
    assert "[high] SQL injection in /login" in done.stdout
    assert "strix -n -t ./ -m quick\n" in calls and "--fail-on" not in calls
    route = "run --no-sync python scripts/strix_findings.py --report strix_runs/run-1/"
    assert route in calls and "--no-issues" in calls


def test_strix_lane_a_clean_scan_writes_no_report_and_passes(tmp_path):
    # ...and writes vulnerabilities.json only when there are findings
    done, calls = run_strix_lane(tmp_path, rc=0, stale=True)
    assert done.returncode == 0 and "scan completed, no findings" in done.stdout
    assert "strix_findings.py" not in calls  # the earlier run's report is not routed again


def test_strix_lane_failures_stay_failures(tmp_path):
    done, _ = run_strix_lane(tmp_path, rc=1)
    assert done.returncode == 1 and "did not complete" in done.stderr
    (tmp_path / "scripts" / "strix_quick_scan.sh").unlink()
    (tmp_path / "scripts").rmdir()
    done, calls = run_strix_lane(tmp_path, rc=2)  # exit 2 with no report: argparse, not findings
    assert done.returncode == 2 and "usage error" in done.stderr
    assert "strix_findings.py" not in calls


def test_strix_lane_blocks_only_when_asked_and_can_skip_routing(tmp_path):
    done, calls = run_strix_lane(
        tmp_path, rc=2, report=ONE_FINDING, STRIX_FAIL_ON="high", STRIX_ROUTE="0"
    )
    assert done.returncode == 2 and "at or above STRIX_FAIL_ON=high" in done.stderr
    assert "strix -n -t ./ -m quick --fail-on high\n" in calls
    assert "strix_findings.py" not in calls  # STRIX_ROUTE=0: printed, not written as lessons
    assert "[high] SQL injection in /login" in done.stdout
    workflow = _text(".forgejo", "workflows", "security.yaml")
    assert 'STRIX_ROUTE: "0"' in workflow  # the runner has no route to the swarm's Postgres


def test_make_strix_and_strix_scan_share_the_script():
    import shutil
    import subprocess

    if shutil.which("make") is None:
        pytest.skip("make not installed")
    for target in ("strix", "strix-scan"):
        # under `make ci-local` this is a nested make: drop its directory banner
        out = subprocess.run(
            ["make", "-n", "--no-print-directory", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out.strip() == "./scripts/strix_quick_scan.sh", (target, out)


def test_no_compose_or_k8s_file_names_the_unpublished_strix_image():
    import glob

    files = glob.glob(os.path.join(ROOT, "deploy", "**", "*.y*ml"), recursive=True)
    assert len(files) > 10
    for path in files:
        with open(path) as f:
            assert "ghcr.io/usestrix/strix:" not in f.read(), path
    assert not os.path.exists(os.path.join(ROOT, "deploy", "k8s", "strix.yaml"))
    services = yaml.safe_load(_text("deploy", "compose.yaml"))["services"]
    assert "strix" not in services
    # no swarm container mounts the host's container socket any more
    for name, service in services.items():
        for volume in service.get("volumes", []):
            assert "docker.sock" not in str(volume), name


@pytest.fixture
def strix_env():
    env_dir = os.environ.get("STRIX_ENV")
    if not env_dir or not os.path.exists(os.path.join(env_dir, "bin", "strix")):
        reason = "STRIX_ENV is not set to an environment synced with --only-group security"
        if os.environ.get("REQUIRE_STRIX_ENV") == "1":
            pytest.fail(f"REQUIRE_STRIX_ENV=1 but {reason}")
        pytest.skip(f"{reason}; `make ci-local` provides it")
    return env_dir


def test_strix_image_is_the_default_the_locked_package_declares(strix_env):
    import subprocess

    code = (
        "from strix.config.settings import RuntimeSettings, TelemetrySettings;"
        "print(RuntimeSettings.model_fields['image'].default);"
        "print(RuntimeSettings.model_fields['image'].alias);"
        "print(TelemetrySettings.model_fields['enabled'].alias)"
    )
    out = subprocess.check_output([os.path.join(strix_env, "bin", "python"), "-c", code])
    image, image_var, telemetry_var = out.decode().split()
    assert image_var == "STRIX_IMAGE" and telemetry_var == "STRIX_TELEMETRY"
    assert image.startswith("ghcr.io/usestrix/strix-sandbox:")
    assert f'STRIX_IMAGE="${{STRIX_IMAGE:-{image}}}"' in _text("scripts", "strix_quick_scan.sh")
    assert f"#STRIX_IMAGE={image}" in _text(".env.example")
    assert f"`{image}`" in _text("conventions", "licensing.md")


def test_strix_flags_the_lane_uses_exist_in_the_locked_cli(strix_env):
    import subprocess

    usage = subprocess.run(
        [os.path.join(strix_env, "bin", "strix"), "--help"], capture_output=True, text=True
    ).stdout
    for flag in ("-n", "-t TARGET", "-m {quick,standard,deep}", "--scope-mode {auto,diff,full}",
                 "--diff-base DIFF_BASE"):  # fmt: skip
        assert flag in usage, flag
    assert "--output" not in usage  # v4.2's `--output json` never existed here


def test_strix_findings_reads_what_strix_agent_writes():
    # strix-agent 1.7.0: strix_runs/<run>/vulnerabilities.json is a top-level list
    report = [
        {"title": "SSRF in fetcher", "severity": "high", "poc_description": "curl http://169…"},
        {"title": "speculative", "severity": "low"},
    ]
    validated = strix_findings.validated_findings(report)
    assert [v["title"] for v in validated] == ["SSRF in fetcher"]
    assert "curl http://169" in strix_findings.finding_lesson(validated[0])


def test_shadow_eval_engines_runs_each_engine_in_its_own_process():
    recipe = _text("Makefile").split("\nshadow-eval-engines:")[1].split("\n\n")[0]
    assert "env AGENT_ENGINE=aider python scripts/shadow_eval.py --single engine-aider" in recipe
    assert "env AGENT_ENGINE=codex python scripts/shadow_eval.py --single engine-codex" in recipe
    assert "--report eval/engine-aider.json eval/engine-codex.json" in recipe
    assert "--candidate AGENT_ENGINE=codex" not in recipe  # ENGINE is read at import: no-op


def test_v42_runtime_checks_are_deferred_not_skipped():
    vi = _text("Makefile").split("\nverify-integration:")[1]
    for needle in ("deploy/Containerfile.codex", "./scripts/codex_egress_audit.sh",
                   "./scripts/strix_quick_scan.sh"):  # fmt: skip
        assert needle in vi, needle
    assert "antidoom" not in vi  # Antidoom stays its own offline target
    for src in (_text(".forgejo", "workflows", "ci.yaml"), _text("scripts", "ci_local.sh")):
        assert "uv run --no-sync python .forgejo/scripts/agents_md_gate.py" in src


def test_every_v42_make_target_survives():
    makefile = _text("Makefile")
    for target in ("strix", "codex-engine", "shadow-eval-engines", "codex-review", "codex-audit",
                   "agents-md-check", "strix-findings", "doomloop"):  # fmt: skip
        assert re.search(rf"^{target}:", makefile, re.M), target


def test_codex_audit_uses_the_keys_and_variables_of_the_pinned_cli():
    # verified in openai/codex at tag rust-v0.167.0 (the pinned @openai/codex)
    src = _text("scripts", "codex_egress_audit.sh")
    assert "check_for_update_on_startup = false" in src
    assert "[analytics]\nenabled = false" in src
    assert "disable_update_check" not in src and "telemetry = false" not in src
    assert "CODEX_OSS_BASE_URL=http://ollama:11434/v1" in src
    # sealed-container condition: the internal network, never --network=none + localhost
    assert '--network "$NETWORK"' in src and "--network=none" not in src
    assert "deploy_swarm-internal" in src and "swarm-codex:dev" in src
    assert "ghcr.io/openai/codex" not in src  # that image name could not be verified
    for code in ("exit 1", "exit 2", "exit 3"):  # FAIL / SKIPPED / INCONCLUSIVE kept
        assert code in src


def test_antidoom_train_passes_only_flags_upstream_defines():
    # upstream's runner (Liquid4All/antidoom) has --model-name, --target-pairs and
    # --output-dir; rank, learning rate, early stopping and merge are config fields
    src = _text("deploy", "antidoom", "train.sh")
    call = src.split("uv run antidoom", 1)[1].split("\n\n", 1)[0]
    flags = set(re.findall(r"--[a-z-]+", call))
    assert flags == {"--model-name", "--target-pairs", "--output-dir"}, flags
    assert "s/^( *lora_r:).*/\\1 ${LORA_R}/" in src and "learning_rate:" in src
    assert "REFUSED: target Ornith only" in src  # the LFM2 guard is kept
