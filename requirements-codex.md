# v4.2 Workstream 7 — Codex CLI engine (tooling note, not a pip dep)

Codex is a **Rust binary**, not a Python package, so it is installed as a
CLI tool (like `aider` is invoked, not imported). This note documents the
install + pin rather than a `requirements-*.txt` entry.

## Install (on the engine host / CI runner — NOT the sealed container)

```bash
# npm (reference distribution) or a pinned GitHub release binary
npm install -g @openai/codex@<pinned-version>
# or download the release binary for your arch and put it on PATH
codex --version   # confirm the pinned version
```

No Node is needed at runtime — only to install the binary; the binary
itself is self-contained.

## Configuration for self-hosted, offline use

Codex drives local models with the reserved provider id `ollama`:

```bash
export CODEX_HOME=/path/to/offline/codex-home   # self-update + telemetry off
codex exec --oss --local-provider ollama --skip-git-repo-check "<task>"
```

`AGENT_ENGINE=codex` in `.env` routes the swarm worker through this path
(`agent/sandbox.py:engine_cmd`). Exact flags are overridable via
`CODEX_EXEC_FLAGS` so a pinned version's surface can change without code.

## License / placement gates

- **License:** verify the `openai/codex` repo license at pin time — used
  as an external tool (invocation, not redistribution), like aider. The
  `make gate` syft scan does not see it (it's not in the Python tree); the
  ledger records the pinned version + license.
- **Egress (T7.1):** before Codex may run inside the sealed agent
  container, `scripts/codex_egress_audit.sh` must PASS (no auth/telemetry
  callout). If it fails, Codex runs on the `codex-engine` lane (T7.3) with
  controlled egress instead — documented, never silent.

## Claude Agent SDK (fallback lane only)

`claude-agent-sdk` (Python, `pip`) is the escalation engine for the hard
5% — Claude-model-only, Anthropic egress, so it lives ONLY on the
API-fallback lane (`agent/escalation_claude.py`, behind
`ROUTE_API_FALLBACK=1`). It is added to `requirements-elevation.txt` as an
opt-in dependency; it never enters the sealed container.
