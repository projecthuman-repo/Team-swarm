"""Sandboxed engine execution with a green-build gate and grounded repair.

The agent container itself is already egress-less (Tier A: internal-only
network; Tier B: default-deny NetworkPolicy + Istio ambient). The engine
(aider baseline, nano-claude-code option) runs headless against the
OpenAI-compatible endpoint — directly (Ollama/vLLM) or through the
Headroom compression proxy (v4.1 T1.3, HEADROOM_ENABLED=1).

v4.1 T2.1/T2.6 — green-build gate + grounded repair loop:
after the engine run, `ruff check` + `pytest -q` execute inside the
workspace. On red, the ACTUAL tool output (never "please re-check
yourself" — intrinsic self-correction is verified not to work) is
appended to a repair message and the engine re-runs, at most
MAX_REPAIR_CYCLES times. Still red -> PoisonError -> no push, DLQ.

Executable *skills* remain a separate, stricter boundary
(skills/run_skill.sh; gVisor/Kata on Tier B). Never on the host kernel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from agent.budget import ValkeyBudget

log = logging.getLogger("sandbox")

FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://forgejo:3000")
FORGEJO_OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
FORGEJO_REPO = os.environ.get("FORGEJO_REPO", "sandbox-repo")
FORGEJO_BOT_USER = os.environ.get("FORGEJO_BOT_USER", "swarm-bot")
MODEL = os.environ.get("AIDER_MODEL", "ollama/maxwell1500/ornith-9b:Q4_K_M")
EDITOR_MODEL = os.environ.get("AIDER_EDITOR_MODEL", MODEL)
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
TASK_TIMEOUT_S = int(os.environ.get("TASK_TIMEOUT_S", "1800"))

# Workspace policy (Lean & Precise L1.6): every task works in a fresh
# `task-*` dir under SANDBOX_WORKDIR (the size-capped tmpfs in the agent
# containers). The dir's mtime is a heartbeat, refreshed before every
# bounded step, so reap_orphans() on worker boot removes only dirs that no
# live task has touched for TASK_TIMEOUT_S (crashed or killed workers).
SANDBOX_WORKDIR = os.environ.get("SANDBOX_WORKDIR") or tempfile.gettempdir()

# Headroom token-compression proxy (v4.1 W1). When enabled, the engine's
# OpenAI-compatible traffic goes agent -> headroom -> (LiteLLM ->) model.
# Budgets keep metering what the engine actually sends (post-compression).
HEADROOM_ENABLED = os.environ.get("HEADROOM_ENABLED", "") == "1"
HEADROOM_URL = os.environ.get("HEADROOM_URL", "http://headroom:8787/v1")

# Green-build gate (v4.1 T2.1)
MAX_REPAIR_CYCLES = int(os.environ.get("MAX_REPAIR_CYCLES", "2"))
QUALITY_CHECK_TIMEOUT_S = int(os.environ.get("QUALITY_CHECK_TIMEOUT_S", "300"))
QUALITY_CHECKS = os.environ.get("QUALITY_CHECKS", "ruff check .;pytest -q")
# Two-stage gate (Lean & Precise L2.1): a fast lint of the changed Python
# files first; red there returns at once (seconds, not a pytest run) and the
# repair prompt carries only that output. Green -> QUALITY_CHECKS + verify_cmd.
# --force-exclude keeps stage 1 a subset of stage 2: ruff ignores its
# exclude/extend-exclude for explicitly passed paths without it.
QUALITY_FAST_CHECK = os.environ.get("QUALITY_FAST_CHECK", "ruff check --force-exclude")

# Reproduction-test-first policy (v4.1 T2.2)
REPRO_TEST_POLICY = os.environ.get("REPRO_TEST_POLICY", "1") == "1"

ENGINE = os.environ.get("AGENT_ENGINE", "aider")  # aider|nano-claude-code
ENGINE_CMD = os.environ.get("AGENT_ENGINE_CMD", "")

# aider prints e.g. "Tokens: 4.2k sent, 1.1k received."
_TOKENS_RE = re.compile(r"Tokens:\s*([\d.]+)(k?)\s*sent,\s*([\d.]+)(k?)\s*received")


def reap_orphans(
    root: str | None = None, max_age_s: float | None = None, now: float | None = None
) -> list[str]:
    """Remove `task-*` workdirs under `root` untouched for `max_age_s`.

    Defaults: SANDBOX_WORKDIR and TASK_TIMEOUT_S. Symlinks are never
    followed. Returns the removed paths.
    """
    root = root or SANDBOX_WORKDIR
    max_age_s = TASK_TIMEOUT_S if max_age_s is None else max_age_s
    now = time.time() if now is None else now
    removed = []
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not entry.name.startswith("task-") or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except FileNotFoundError:
            continue  # removed concurrently
        if age > max_age_s:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed.append(entry.path)
    if removed:
        log.warning("reaped %d orphaned task workdir(s) under %s", len(removed), root)
    return removed


def _heartbeat(workdir: str) -> None:
    """Mark a live workdir so reap_orphans() leaves it alone."""
    try:
        os.utime(workdir)
    except FileNotFoundError:
        pass


class PoisonError(Exception):
    """Non-transient task failure: terminate -> MAX_DELIVERIES -> DLQ."""


@dataclass
class RunResult:
    """Outcome of one sandboxed engine run (v4.1 quality metrics)."""

    first_pass_green: bool = True
    repair_cycles: int = 0
    gate_stage1_ms: int | None = None  # first-pass gate timings (L2.1)
    gate_stage2_ms: int | None = None
    tokens_spent: int = 0
    check_output: str = ""
    branch: str = ""
    extra: dict = field(default_factory=dict)


def engine_cmd(message: str) -> list[str]:
    """Build the agent-engine command for this task's message."""
    if ENGINE_CMD:
        return [message if part == "{message}" else part for part in shlex.split(ENGINE_CMD)]
    if ENGINE == "nano-claude-code":
        return ["nano-claude-code", "--prompt", message]
    if ENGINE == "aider":
        return [
            "aider",
            "--yes-always",
            "--no-analytics",
            "--architect",
            "--model",
            MODEL,
            "--editor-model",
            EDITOR_MODEL,
            "--message",
            message,
        ]
    raise PoisonError(f"unknown AGENT_ENGINE {ENGINE!r}")


def build_message(task) -> str:
    """Task message with the repro-test-first policy (T2.2) applied."""
    message = task.title or f"work task {task.task_id}"
    if task.spec_ref:
        message += f"\n\nFull spec: artifact {task.spec_ref} (SeaweedFS)."
    if task.verify_cmd:
        message += f"\n\nSuccess criterion: `{task.verify_cmd}` must pass."
    if REPRO_TEST_POLICY:
        template = _read_convention("repro-tests.md")
        if template:
            message += "\n\n" + template
    return message


def _read_convention(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "conventions", name)
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def repair_message(original: str, check_output: str, cycle: int) -> str:
    """Grounded repair prompt: the ACTUAL failing tool output, verbatim.

    Never asks the model to 're-check itself' — external signal only
    (v4.1 T2.1; intrinsic self-correction is verified not to work).
    """
    return (
        f"{original}\n\n"
        f"--- REPAIR CYCLE {cycle}/{MAX_REPAIR_CYCLES} ---\n"
        "Your previous change fails the project's quality checks. The exact "
        "tool output is below. Fix ONLY what it reports; do not refactor "
        "anything else.\n\n"
        f"```\n{check_output[-4000:]}\n```"
    )


def _parse_tokens(output: str) -> int:
    total = 0
    for sent, sk, recv, rk in _TOKENS_RE.findall(output):
        total += int(float(sent) * (1000 if sk else 1))
        total += int(float(recv) * (1000 if rk else 1))
    return total


def build_env() -> dict:
    """Engine subprocess environment, honoring the Headroom proxy switch."""
    env = {
        **os.environ,
        "OLLAMA_API_BASE": OLLAMA_API_BASE,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if HEADROOM_ENABLED:
        # aider/litellm honor OPENAI_API_BASE for openai/<model> names;
        # the proxy forwards to Ollama/vLLM (or LiteLLM) upstream.
        env["OPENAI_API_BASE"] = HEADROOM_URL
        env["OPENAI_BASE_URL"] = HEADROOM_URL
    return env


async def _run(
    cmd: list[str], cwd: str, env: dict, timeout: int, check: bool = True
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as err:
        proc.kill()
        raise TimeoutError(f"{cmd[0]} exceeded {timeout}s") from err
    out = out_bytes.decode(errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}:\n{out[-2000:]}")
    return proc.returncode or 0, out


async def changed_python_files(repo: str, env: dict, base: str = "HEAD") -> list[str]:
    """Python files added/copied/modified/renamed since `base`, plus untracked ones.

    `base` is the commit the task branch started from: aider auto-commits
    its edits, so a diff against HEAD would miss them (DECISIONS D-030).
    The diff covers committed and uncommitted changes alike.
    """
    _, diff = await _run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base], repo, env, 60, check=False
    )
    _, untracked = await _run(
        ["git", "ls-files", "--others", "--exclude-standard"], repo, env, 60, check=False
    )
    files = []
    for line in (diff + "\n" + untracked).splitlines():
        path = line.strip()
        if path.endswith(".py") and path not in files:
            files.append(path)
    return files


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


async def check_workspace(
    repo: str,
    env: dict,
    verify_cmd: str = "",
    timings: dict | None = None,
    base: str = "HEAD",
) -> tuple[bool, str]:
    """Run the two-stage quality gate (T2.1, L2.1). Returns (green, output).

    Stage 1: QUALITY_FAST_CHECK on the .py files changed since `base` (the
    task's starting commit); red returns
    immediately with just that output. Skipped when no Python changed.
    Stage 2: every QUALITY_CHECKS command, then verify_cmd (T3.1).
    pytest exit code 5 (no tests collected) counts as green; a check
    binary missing from the workspace image is skipped with a note.
    `timings`, when given, receives stage1_ms / stage2_ms (None = not run).
    """
    timings = {} if timings is None else timings
    timings["stage1_ms"] = timings["stage2_ms"] = None

    files = await changed_python_files(repo, env, base) if QUALITY_FAST_CHECK.strip() else []
    if files:
        start = time.monotonic()
        argv = shlex.split(QUALITY_FAST_CHECK) + files
        shown = f"{QUALITY_FAST_CHECK} {' '.join(shlex.quote(f) for f in files)}"
        try:
            code, out = await _run(argv, repo, env, QUALITY_CHECK_TIMEOUT_S, check=False)
        except FileNotFoundError:
            code, out = None, f"(skipped: {argv[0]} not installed)"
        else:
            timings["stage1_ms"] = _ms_since(start)
        if code:
            return False, f"$ {shown}\n{out}"
        stage1_note = [f"$ {shown}\n{out}"] if code is None else []
    else:
        stage1_note = []

    start = time.monotonic()
    outputs = stage1_note
    green = True
    for check_cmd in [c.strip() for c in QUALITY_CHECKS.split(";") if c.strip()]:
        argv = shlex.split(check_cmd)
        try:
            code, out = await _run(argv, repo, env, QUALITY_CHECK_TIMEOUT_S, check=False)
        except FileNotFoundError:
            outputs.append(f"$ {check_cmd}\n(skipped: {argv[0]} not installed)")
            continue
        outputs.append(f"$ {check_cmd}\n{out}")
        if argv[0] == "pytest" and code == 5:
            continue  # no tests collected — nothing to fail
        if code != 0:
            green = False
    if verify_cmd:  # per-subtask verification (T3.1)
        code, vout = await _run(
            shlex.split(verify_cmd), repo, env, QUALITY_CHECK_TIMEOUT_S, check=False
        )
        green = green and code == 0
        outputs.append(f"$ {verify_cmd}\n{vout}")
    timings["stage2_ms"] = _ms_since(start)
    return green, "\n\n".join(outputs)


async def run_sandboxed_aider(task, secret: str, budget: ValkeyBudget) -> RunResult:
    """Clone, run the engine with the green gate + repair loop, push.

    The Forgejo token is a short-lived OpenBao lease, used only in the
    remote URL for this process; a red final result NEVER pushes.
    """
    branch = task.branch or f"feature/{task.task_id}"
    remote = (
        f"{FORGEJO_URL.split('://', 1)[0]}://{FORGEJO_BOT_USER}:{secret}@"
        f"{FORGEJO_URL.split('://', 1)[1]}/{FORGEJO_OWNER}/{FORGEJO_REPO}.git"
    )
    os.makedirs(SANDBOX_WORKDIR, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix=f"task-{task.task_id[:8]}-", dir=SANDBOX_WORKDIR)
    env = build_env()
    message = build_message(task)
    result = RunResult(branch=branch)

    try:
        await _run(["git", "clone", "--depth", "1", remote, "repo"], workdir, env, 300)
        repo = os.path.join(workdir, "repo")
        await _run(["git", "checkout", "-b", branch], repo, env, 60)
        _, base = await _run(["git", "rev-parse", "HEAD"], repo, env, 60)
        base = base.strip()  # the gate's stage 1 diffs against this (D-030)

        prompt = message
        green = False
        check_out = ""
        for cycle in range(MAX_REPAIR_CYCLES + 1):
            _heartbeat(workdir)
            _, out = await _run(engine_cmd(prompt), repo, env, TASK_TIMEOUT_S)
            result.tokens_spent += max(_parse_tokens(out), 1)
            await budget.spend(max(_parse_tokens(out), 1))

            _heartbeat(workdir)
            timings: dict = {}
            green, check_out = await check_workspace(repo, env, task.verify_cmd, timings, base)
            if cycle == 0:
                result.first_pass_green = green
                result.gate_stage1_ms = timings["stage1_ms"]
                result.gate_stage2_ms = timings["stage2_ms"]
            if green:
                break
            result.repair_cycles = cycle + 1
            if cycle < MAX_REPAIR_CYCLES:
                log.warning(
                    "task %s red, repair cycle %d/%d",
                    task.task_id,
                    cycle + 1,
                    MAX_REPAIR_CYCLES,
                )
                prompt = repair_message(message, check_out, cycle + 1)

        result.check_output = check_out[-4000:]
        if not green:
            # T2.6: repair cap exceeded -> DLQ -> needs-human. Never push red.
            raise PoisonError(
                f"red after {MAX_REPAIR_CYCLES} repair cycle(s); not pushing. "
                f"Last check output:\n{check_out[-1500:]}"
            )

        # Bot pushes ONLY to feature/* (Hard Rule 4); auto-merge to
        # integration happens via the Forgejo API after checks pass.
        if not branch.startswith("feature/"):
            raise PoisonError(f"refusing to push non-feature branch {branch!r}")
        _heartbeat(workdir)
        await _run(["git", "push", "-u", "origin", branch], repo, env, 300)
        return result
    except (RuntimeError, FileNotFoundError) as err:
        # Deterministic tool failure -> let JetStream retry; after
        # max-deliver the advisory routes it to the DLQ.
        raise PoisonError(str(err)) from err
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
