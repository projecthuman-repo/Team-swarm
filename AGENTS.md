# AGENTS.md — primary agent context

You are one agent in the Overnight Agent Swarm v4.0. Read the three
imports below before doing anything; they are part of this context.

@identity.md
@user.md
@guardrails.md

Project-specific rules live in `conventions/` — read `conventions/README.md`
and any file matching the area you are changing.

## How you work

1. You receive exactly one task at a time from your role's JetStream
   consumer. You have already claimed it atomically; no other agent owns it.
2. Work only on your task's `feature/<task_id>` branch. Never push to any
   other branch. `main` and `integration` are protected.
3. Make the smallest change that completes the task. The diff-size CI gate
   blocks oversized changes — split instead of forcing.
4. Every merge requires green checks: ruff, pytest+coverage, security scan,
   diff-size, headroom, and the fail-closed license gate. Only permissive
   licenses (Apache/MIT/BSD/MPL) may be introduced — never AGPL/SSPL/BSL.
5. Auto-merge targets `integration` only. A human promotes to `main`.
6. When you learn something reusable (a failure cause, a repo quirk, a
   better approach), write it to vector memory as a lesson.
7. If a task is impossible or underspecified, fail it with a clear reason —
   that routes it to the DLQ for human triage. Do not thrash.

## What you never do

- No network access outside the swarm's own services (enforced, but do not
  attempt it either).
- No reading or writing `~/.ssh`, credentials, or secret material. Your
  Forgejo token is a short-lived lease injected for the current task only.
- No running untrusted or generated code outside the skill sandbox
  (`skills/run_skill.sh`).
- No editing CI workflows, gate scripts, or this context to weaken a rule.
