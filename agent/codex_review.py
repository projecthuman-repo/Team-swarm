"""Codex review as a second, heterogeneous reviewer (v4.2 T7.4).

Codex reviews a diff and its findings are mapped into the finding shape
agent/verifier.py uses, so aggregate() treats Codex as one more reviewer
lens. Verified finding: heterogeneous reviewers (a different engine/model)
beat homogeneous ones — Codex-on-Ornith complements the Ornith lenses.

Codex runs self-hosted via --oss; it is invoked as an external tool (like
aider), never imported.

How the review is run (checked against the pinned CLI, rust-v0.167.0):

    codex exec <CODEX_REVIEW_FLAGS> -m <model> -s read-only
        --skip-git-repo-check --ephemeral --json
        --output-schema <schema> "<review prompt with the diff in it>"

The diff against the merge base is computed here and put into the prompt:
Codex's read-only sandbox needs user namespaces, so in the swarm's hardened
containers the reviewer cannot run `git diff` itself.

`codex exec review --base` is NOT used. Its `--json` stream carries the
review only as rendered text in an `agent_message` item — no finding list,
no priority field, no verdict — so a clean review cannot be told from
reviewer prose that failed to parse, and nothing in it can say PASS. With
`--output-schema` the final `agent_message` is a JSON document, which this
module validates strictly.

Fail-closed: the verdict is PASS only for a completed turn whose final
message is a valid document saying "patch is correct" with no finding at
or above the blocking priority. An absent `codex` binary, a non-zero exit,
a `turn.failed` or `error` event, a stream that does not end in
`turn.completed`, a missing or malformed document — each is one ERROR
finding, never a PASS.

    python -m agent.codex_review --base integration            # in a repo
    python -m agent.codex_review --base integration --json-out findings.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

log = logging.getLogger("codex-review")

# `--oss` alone is rejected by the pinned CLI ("No default OSS provider
# configured"): the provider must be named.
CODEX_REVIEW_FLAGS = os.environ.get("CODEX_REVIEW_FLAGS", "--oss --local-provider ollama")
REVIEW_TIMEOUT_S = int(os.environ.get("CODEX_REVIEW_TIMEOUT_S", "600"))
# Findings at this priority or more urgent block (0 = P0 … 3 = P3).
BLOCK_PRIORITY = int(os.environ.get("CODEX_REVIEW_BLOCK_PRIORITY", "1"))

CORRECT, INCORRECT = "patch is correct", "patch is incorrect"
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "overall_correctness", "overall_explanation"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "body", "priority", "file", "line"],
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
            },
        },
        "overall_correctness": {"type": "string", "enum": [CORRECT, INCORRECT]},
        "overall_explanation": {"type": "string"},
    },
}
# The diff is in the prompt: the reviewer must not depend on running `git
# diff` itself. Codex's read-only sandbox is bubblewrap with user namespaces;
# in the swarm's hardened containers it cannot start, every command fails,
# and a reviewer that saw nothing could still answer "patch is correct".
MAX_DIFF_CHARS = int(os.environ.get("CODEX_REVIEW_MAX_DIFF_CHARS", "60000"))
REVIEW_PROMPT = (
    "Review the code changes below: the complete diff of this repository against "
    "the base commit {base} (committed and uncommitted work alike). You may read "
    "files for context; the diff is what you judge. "
    "Report only defects the author would fix: bugs, security problems, behaviour "
    "changes without a test. Give each finding a priority: 0 = blocks the merge, "
    "1 = urgent, 2 = normal, 3 = nit. Reply with one JSON object and nothing else: "
    '"findings" (each with title, body, priority, file, line), "overall_correctness" '
    f'("{CORRECT}" or "{INCORRECT}") and "overall_explanation".'
    "\n\n```diff\n{diff}\n```"
)


class ReviewFailed(Exception):
    """The review did not produce a result that can be judged."""


def codex_available() -> bool:
    return shutil.which("codex") is not None


def review_model() -> str:
    """The model Codex is told to use: CODEX_MODEL, else the swarm's engine
    model without aider's `ollama/` prefix. Never Codex's own OSS default."""
    from agent.sandbox import codex_model

    return codex_model()


def merge_base(base: str, cwd: str = ".") -> str:
    """The commit the review diffs against: the merge base of HEAD and `base`."""
    for cmd in (["git", "merge-base", "HEAD", base], ["git", "rev-parse", "--verify", base]):
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise ReviewFailed(f"cannot resolve the review base {base!r} in {cwd}")


def diff_against(sha: str, cwd: str = ".") -> str:
    """Everything that differs from `sha`: committed, uncommitted and new
    files. Uses a scratch index, so the repository's own index is untouched."""
    with tempfile.TemporaryDirectory(prefix="codex-review-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        for cmd in (["git", "read-tree", "HEAD"], ["git", "add", "-A"]):
            subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        proc = subprocess.run(
            ["git", "diff", "--cached", sha, "--", ".", ":(exclude,glob)**/__pycache__/**"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if proc.returncode != 0:
        raise ReviewFailed(f"git diff against {sha[:12]} failed: {proc.stderr[-200:]}")
    if not proc.stdout.strip():
        raise ReviewFailed(f"nothing to review: no difference from {sha[:12]}")
    if len(proc.stdout) > MAX_DIFF_CHARS:
        # a verdict on part of a change is not a verdict on the change
        raise ReviewFailed(
            f"the diff ({len(proc.stdout)} characters) is larger than the reviewer is "
            f"given (CODEX_REVIEW_MAX_DIFF_CHARS={MAX_DIFF_CHARS})"
        )
    return proc.stdout


def run_codex_review(base: str, cwd: str = ".") -> str:
    """Run the review; returns the raw `--json` event stream (stdout).

    A non-zero exit raises: a reviewer that crashed has reviewed nothing.
    stdin is closed — `codex exec` appends piped stdin to the prompt.
    """
    sha = merge_base(base, cwd)
    diff = diff_against(sha, cwd)
    with tempfile.TemporaryDirectory(prefix="codex-review-") as tmp:
        schema = os.path.join(tmp, "review.schema.json")
        with open(schema, "w") as f:
            json.dump(REVIEW_SCHEMA, f)
        cmd = [
            "codex",
            "exec",
            *shlex.split(CODEX_REVIEW_FLAGS),
            "-m",
            review_model(),
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "--output-schema",
            schema,
            REVIEW_PROMPT.format(base=sha, diff=diff),
        ]
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=REVIEW_TIMEOUT_S,
        )
    if proc.returncode != 0:
        raise ReviewFailed(
            f"codex review exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}"
        )
    return proc.stdout


def final_message(stream: str) -> str:
    """The final agent message of a completed `codex exec --json` turn.

    Event shapes are exec_events.rs `ThreadEvent`: thread.started,
    turn.started, turn.completed, turn.failed, item.started|updated|completed
    (item.type agent_message carries `text`), error. Anything but a turn
    that completed cleanly raises.
    """
    events = []
    for line in stream.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as err:
            raise ReviewFailed(f"not a JSON event line: {line[:120]!r}") from err
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ReviewFailed(f"not an event: {line[:120]!r}")
        events.append(event)
    if not events:
        raise ReviewFailed("codex printed no events")
    for event in events:
        if event["type"] == "turn.failed":
            reason = (event.get("error") or {}).get("message", "no detail")
            raise ReviewFailed(f"turn failed: {reason}")
        if event["type"] == "error":
            raise ReviewFailed(f"error event: {event.get('message', 'no detail')}")
    if events[-1]["type"] != "turn.completed":
        raise ReviewFailed(f"the stream ends with {events[-1]['type']}, not turn.completed")
    messages = [
        e["item"].get("text")
        for e in events
        if e["type"] == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"].get("type") == "agent_message"
    ]
    if not messages or not isinstance(messages[-1], str):
        raise ReviewFailed("the turn completed without an agent message")
    return messages[-1]


def judge_document(text: str) -> dict:
    """One finding for aggregate() from the review document (REVIEW_SCHEMA).

    Validated here whatever the provider did with the schema: PASS needs a
    well-formed document that says so.
    """
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError) as err:
        raise ReviewFailed("the final message is not a JSON document") from err
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise ReviewFailed("the review document has no findings list")
    if doc.get("overall_correctness") not in (CORRECT, INCORRECT):
        raise ReviewFailed("the review document does not say whether the patch is correct")
    for item in doc["findings"]:
        priority = item.get("priority") if isinstance(item, dict) else None
        if type(priority) is not int or not 0 <= priority <= 3:
            raise ReviewFailed("a finding has no priority between 0 and 3")
        if not isinstance(item.get("title"), str):
            raise ReviewFailed("a finding has no title")
    blocking = [it for it in doc["findings"] if it["priority"] <= BLOCK_PRIORITY]
    blocked = bool(blocking) or doc["overall_correctness"] == INCORRECT
    shown = blocking or doc["findings"]
    notes = "; ".join(
        f"[P{it['priority']}] {it['title']} ({it.get('file', '?')}:{it.get('line', '?')})"[:200]
        for it in shown[:10]
    )
    notes = notes or str(doc.get("overall_explanation") or "no findings")
    return {"lens": "codex", "verdict": "BLOCK" if blocked else "PASS", "notes": notes[:1500]}


def _error(reason: str) -> list[dict]:
    return [{"lens": "codex", "verdict": "ERROR", "notes": reason[:300]}]


def _is_finding(item) -> bool:
    """A finding object, not an event (`type`) or an error payload."""
    return (
        isinstance(item, dict)
        and "type" not in item
        and any(key in item for key in ("title", "priority", "severity"))
    )


def parse_findings(raw: str) -> list[dict]:
    """Map a findings document in v4.2's shape to a verifier finding.

    That shape is `{"findings": [...]}`, a bare list, or one finding object
    per line, with a string `priority`/`severity`; high/critical/blocker/
    error blocks. Kept for callers that already hold such a document — the
    live review goes through final_message() and judge_document().

    Only a recognised finding list (possibly empty) can be PASS. Anything
    else — no JSON, `null`, a number, an error object, a `codex exec --json`
    event stream — is ERROR (review of R7: those used to be PASS).
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # one JSON object per line
        data = []
        for line in raw.splitlines() if isinstance(raw, str) else []:
            line = line.strip()
            if line.startswith("{"):
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    return _error("codex review output has a line that is not JSON")
        if not data:
            return _error("codex review produced no parseable JSON findings")

    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        items = data["findings"]
    elif isinstance(data, list):
        items = data
    else:
        return _error("codex review output is JSON but not a finding list")
    if not all(_is_finding(it) for it in items):
        return _error("codex review output is not a list of findings")
    blocking = [
        it
        for it in items
        if str(it.get("priority", it.get("severity", ""))).lower()
        in {"high", "critical", "blocker", "error"}
    ]
    verdict = "BLOCK" if blocking else "PASS"
    notes = (
        "; ".join(str(it.get("title") or it.get("message") or it)[:200] for it in items[:10])
        or "no findings"
    )
    return [{"lens": "codex", "verdict": verdict, "notes": notes[:1500]}]


def review(base: str, cwd: str = ".") -> list[dict]:
    """One Codex-review finding for aggregate(); ERROR whenever the review
    did not produce a result that can be judged."""
    if not codex_available():
        log.warning("codex binary not installed; skipping Codex review lane")
        return _error("codex CLI not installed (T7.4 optional lane)")
    try:
        return [judge_document(final_message(run_codex_review(base, cwd)))]
    except (ReviewFailed, subprocess.SubprocessError, OSError) as err:
        return _error(str(err))


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="integration")
    p.add_argument("--cwd", default=".")
    p.add_argument("--json-out", help="write findings JSON to this path")
    args = p.parse_args()
    findings = review(args.base, args.cwd)
    out = json.dumps(findings, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as f:
            f.write(out)
    print(out)
    # exit non-zero if any lens blocks (feeds CI / aggregate)
    sys.exit(1 if any(f["verdict"] != "PASS" for f in findings) else 0)


if __name__ == "__main__":
    main()
