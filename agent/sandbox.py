"""Sandboxed aider execution.

The agent container itself is already egress-less (Tier A: podman
--network=none derivative with only in-swarm hosts reachable; Tier B:
default-deny NetworkPolicy + Istio ambient). aider runs headless
(--message) with Ornith-1.0 over the OpenAI-compatible endpoint
(Ollama on Tier A, vLLM on Tier B).

Executable *skills* are a separate, stricter boundary: they always go
through skills/run_skill.sh (gVisor/Kata on Tier B, podman
--network=none --read-only --cap-drop=ALL on Tier A). Never run a skill
on the host kernel (Hard Rule 6).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile

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

# aider prints e.g. "Tokens: 4.2k sent, 1.1k received."
_TOKENS_RE = re.compile(r"Tokens:\s*([\d.]+)(k?)\s*sent,\s*([\d.]+)(k?)\s*received")


class PoisonError(Exception):
    """Non-transient task failure: terminate -> MAX_DELIVERIES -> DLQ."""


def _parse_tokens(output: str) -> int:
    total = 0
    for sent, sk, recv, rk in _TOKENS_RE.findall(output):
        total += int(float(sent) * (1000 if sk else 1))
        total += int(float(recv) * (1000 if rk else 1))
    return total


async def _run(cmd: list[str], cwd: str, env: dict, timeout: int) -> str:
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
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}:\n{out[-2000:]}")
    return out


async def run_sandboxed_aider(task, secret: str, budget: ValkeyBudget) -> None:
    """Clone the target repo, run aider on feature/<task_id>, push the branch.

    The Forgejo token is a short-lived OpenBao lease, used only in the
    remote URL for this process; it is never written to any file that
    outlives the workspace.
    """
    branch = task.branch or f"feature/{task.task_id}"
    remote = (
        f"{FORGEJO_URL.split('://', 1)[0]}://{FORGEJO_BOT_USER}:{secret}@"
        f"{FORGEJO_URL.split('://', 1)[1]}/{FORGEJO_OWNER}/{FORGEJO_REPO}.git"
    )
    workdir = tempfile.mkdtemp(prefix=f"task-{task.task_id[:8]}-")
    env = {
        **os.environ,
        "OLLAMA_API_BASE": OLLAMA_API_BASE,
        "GIT_TERMINAL_PROMPT": "0",
    }
    message = task.title or f"work task {task.task_id}"
    if task.spec_ref:
        message += f"\n\nFull spec: artifact {task.spec_ref} (SeaweedFS)."

    try:
        await _run(["git", "clone", "--depth", "1", remote, "repo"], workdir, env, 300)
        repo = os.path.join(workdir, "repo")
        await _run(["git", "checkout", "-b", branch], repo, env, 60)

        out = await _run(
            [
                "aider",
                "--yes-always",
                "--no-analytics",
                "--architect",
                "--model", MODEL,
                "--editor-model", EDITOR_MODEL,
                "--message", message,
            ],
            repo,
            env,
            TASK_TIMEOUT_S,
        )
        await budget.spend(max(_parse_tokens(out), 1))

        # Bot pushes ONLY to feature/* (Hard Rule 4); auto-merge to
        # integration happens via the Forgejo API after checks pass.
        if not branch.startswith("feature/"):
            raise PoisonError(f"refusing to push non-feature branch {branch!r}")
        await _run(["git", "push", "-u", "origin", branch], repo, env, 300)
    except (RuntimeError, FileNotFoundError) as err:
        # Deterministic tool failure -> let JetStream retry; after
        # max-deliver the advisory routes it to the DLQ.
        raise PoisonError(str(err)) from err
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
