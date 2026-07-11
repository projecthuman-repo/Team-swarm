"""Claude Code / Agent SDK escalation on the API-fallback lane (v4.2 T7.7).

The hard ~5% of tasks the local Ornith tier fails can be escalated to
Claude Opus 4.8 via the Claude Agent SDK. This is DELIBERATELY confined to
the API-fallback lane (DR-3 / T4.3):

  - it is Claude-model-only and needs Anthropic egress, so it CANNOT run
    in the sealed agent container (Hard Rule 1);
  - it only runs behind ROUTE_API_FALLBACK=1 (explicit opt-in);
  - the Anthropic key is an OpenBao lease (Hard Rule 7), never in env;
  - the returned patch goes back through the SAME green-build gate (T2.1)
    as any other engine before a PR — escalation buys a better attempt,
    not a bypass.

claude-agent-sdk is optional (opt-in requirements); absent, this raises a
clear SystemExit rather than an ImportError, and the task simply stays on
the normal local retry/DLQ path.

    ROUTE_API_FALLBACK=1 python -m agent.escalation_claude <task_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

log = logging.getLogger("escalation-claude")

ROUTE_API_FALLBACK = os.environ.get("ROUTE_API_FALLBACK", "0") == "1"
CLAUDE_MODEL = os.environ.get("ESCALATION_MODEL", "claude-opus-4-8")
CLAUDE_SECRET = os.environ.get("ESCALATION_SECRET_NAME", "claude")


class EscalationUnavailable(RuntimeError):
    """Raised when escalation is disabled or its optional dep is absent."""


def _load_sdk():
    try:
        import claude_agent_sdk  # noqa: F401

        return claude_agent_sdk
    except ImportError as err:
        raise EscalationUnavailable(
            "claude-agent-sdk not installed (optional; see "
            "requirements-elevation.txt). Task stays on the local retry path."
        ) from err


def escalation_enabled() -> bool:
    """Structural gate: escalation exists ONLY behind the opt-in flag."""
    return ROUTE_API_FALLBACK


def build_prompt(spec: str, failing_diff: str, check_output: str) -> str:
    return (
        "A local coding agent failed this task after its repair budget. "
        "Produce a corrected unified diff. Address the failing checks "
        "exactly; keep the change surgical.\n\n"
        f"# Task spec\n{spec}\n\n"
        f"# Last (failing) attempt\n{failing_diff}\n\n"
        f"# Failing check output\n{check_output[-3000:]}\n"
    )


async def escalate(spec: str, failing_diff: str, check_output: str) -> str:
    """Run the SDK query() against Claude; return the proposed patch text.

    The result is NOT applied here — the caller feeds it back through the
    green-build gate (run_sandboxed_aider-equivalent) before any PR.
    """
    if not escalation_enabled():
        raise EscalationUnavailable(
            "ROUTE_API_FALLBACK=0: escalation is off; fail closed to local retry."
        )
    sdk = _load_sdk()

    # Key as an OpenBao lease (Hard Rule 7) — set for the SDK's transport.
    from agent.openbao import lease_secret

    try:
        os.environ.setdefault("ANTHROPIC_API_KEY", lease_secret(CLAUDE_SECRET, field="api_key"))
    except Exception as err:  # noqa: BLE001 - lease failure must be explicit
        raise EscalationUnavailable(f"could not lease Claude key: {err}") from err

    prompt = build_prompt(spec, failing_diff, check_output)
    chunks: list[str] = []
    # claude-agent-sdk exposes an async query(); options carry the model.
    options = sdk.ClaudeAgentOptions(model=CLAUDE_MODEL) if hasattr(
        sdk, "ClaudeAgentOptions"
    ) else None
    async for message in sdk.query(prompt=prompt, options=options):
        text = getattr(message, "result", None) or getattr(message, "text", None)
        if text:
            chunks.append(str(text))
    return "\n".join(chunks)


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", default="")
    p.add_argument("--diff", default="")
    p.add_argument("--checks", default="")
    args = p.parse_args()
    try:
        patch = asyncio.run(escalate(args.spec, args.diff, args.checks))
    except EscalationUnavailable as err:
        raise SystemExit(str(err)) from err
    print(patch)


if __name__ == "__main__":
    main()
