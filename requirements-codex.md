# v4.2 Workstream 7 — Codex CLI engine (tooling note, not a pip dep)

Codex is a **Rust binary**, not a Python package, so it is installed as a
CLI tool (like `aider` is invoked, not imported). This note documents the
install + pin rather than a `requirements-*.txt` entry.

## Install (on the engine host / CI runner — NOT the sealed container)

```bash
# npm (reference distribution); the swarm pins 0.167.0 (Apache-2.0)
npm install -g @openai/codex@0.167.0
codex --version   # confirm the pinned version
```

The npm package installs `codex` as a Node launcher (`bin/codex.js`) around
a per-platform binary, so **Node (>= 16) is needed at runtime**. The lane
image, `deploy/Containerfile.codex`, pins the same version
(`deploy/codex/package-lock.json`, checked against `ARG CODEX_VERSION`) and keeps
the `node` binary, copied from `node:22-slim` — not npm or corepack. The
build also removes two LGPL components the Linux platform package bundles
and this lane does not use (bubblewrap, the voice runtime).

## Configuration for self-hosted, offline use

Codex drives local models with the reserved provider id `ollama`:

```bash
export CODEX_HOME=/path/to/offline/codex-home   # must exist; see deploy/codex/config.toml
export CODEX_OSS_BASE_URL=http://localhost:11434/v1   # the `ollama` provider (Ollama >= 0.13.4)
codex exec --oss --local-provider ollama -m maxwell1500/ornith-9b:Q4_K_M \
  --sandbox danger-full-access --skip-git-repo-check --color never "<task>"
```

Without `-m` the CLI selects its own default (`gpt-oss:20b`) and asks Ollama
to pull it. `--sandbox danger-full-access` because Codex's sandbox is
bubblewrap with user namespaces: inside the swarm's hardened containers it
cannot start, and the default mode (read-only) cannot edit files. Use it
only where something else is the sandbox — in the swarm, the container.

`AGENT_ENGINE=codex` in `.env` routes the swarm worker through this path
(`agent/sandbox.py:engine_cmd`). Exact flags are overridable via
`CODEX_EXEC_FLAGS` so a pinned version's surface can change without code.

## License / placement gates

- **License:** Apache-2.0 (`@openai/codex` 0.167.0 and its platform
  packages). The lane image ships it, so it is Zone C: the image installs
  exactly `deploy/codex/package-lock.json`, and the license gate scans that
  lock (`conventions/licensing.md`).
- **Egress (T7.1):** before Codex may run inside the sealed agent
  container, `scripts/codex_egress_audit.sh` must PASS (no auth/telemetry
  callout). If it fails, Codex runs on the `codex-engine` lane (T7.3) with
  controlled egress instead — documented, never silent.

## Claude Agent SDK (fallback lane only)

`claude-agent-sdk` (Python, `pip`) is the escalation engine for the hard
5% — Claude-model-only, Anthropic egress, so it lives ONLY on the
API-fallback lane (`agent/escalation_claude.py`, behind
`ROUTE_API_FALLBACK=1`). It is the optional extra `escalation` in
`pyproject.toml`, pinned in `uv.lock` (`uv sync --extra escalation`, or the
generated `requirements-escalation.txt` on top of `requirements.txt`); it
never enters the sealed container or any image. Its wheel bundles the Claude
Code CLI, which is under Anthropic's Commercial Terms rather than the SDK's
MIT license (`conventions/licensing.md`).
