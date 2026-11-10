# AGENTS.md — the shared cross-tool constitution (v4.2 T7.5)

`AGENTS.md` is an open, cross-tool convention (8+ tools). In this swarm it
is the single constitution that governs every engine identically:

- **aider** reads it via `.aider.conf.yml` `read:` (it lists AGENTS.md and
  the four imports).
- **Codex** reads it natively from the repo root (`AGENT_ENGINE=codex`).
- **the API-fallback lane** (Claude Agent SDK escalation) is handed the
  same file as context.

## The contract

1. **One file, one behavior.** A change to how agents behave goes in
   `AGENTS.md` (or an import / `conventions/` file it references), never
   in per-engine config. If two engines behave differently on the same
   task, the fix is here, not in a wrapper.
2. **Size cap.** Keep `AGENTS.md` **≤ 32 KiB** — Codex's
   `project_doc_max_bytes` default. Beyond that Codex truncates and the
   constitution diverges across engines. `make agents-md-check` (and the
   CI gate) enforce this.
3. **Imports stay small.** `identity.md`, `user.md`, `guardrails.md`,
   `karpathy-guidelines.md` are imported; detailed area rules live in
   `conventions/` and are pulled in as needed, not inlined.

## Verify

`make agents-md-check` passes (file ≤ 32 KiB); the same `AGENTS.md`
produces the same behavior on an aider run and a Codex run of a fixture
task (T7.5 acceptance).
