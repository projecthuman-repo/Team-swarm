# Lean & Precise — decision log

Decisions taken without pausing (the prompt is pre-approved). Newest last.

**D-001 · Base branch.** No `integration` branch exists on the remote, so
the base is `main` (at `8e8cbbc`, the merge of the v4.1 tree). The working
branch `feature/lean-precise` was created from it. The v4.2 "Elevation+"
commit `ce2edd9` exists only on `claude/agent-swarm-repo-taafee` and is not
part of the base tree; it is neither modified nor dropped by this loop.
Merging both later will need ordinary conflict resolution in
`agent/sandbox.py`, `deploy/compose.yaml`, `Makefile`, `.env.example`.

**D-002 · uv tag.** `uv --version` = 0.11.32, so every Containerfile copies
uv from `ghcr.io/astral-sh/uv:0.11` (manifest verified, HTTP 200). The
`uv-lock` pre-commit hook is pinned to the matching `0.11.32` tag so the
hook can never re-lock with a different uv and churn `uv.lock`.

**D-003 · Python.** The session's system Python is 3.11; Python 3.12.13 was
installed with `uv python install 3.12` and every check runs on it.

**D-004 · Tools that cannot be installed here.** Piping the syft remote
installer into a shell is blocked by this session's policy, and the same
applies to running downloaded Go release binaries (percli) — so the
license gate (`syft`) and `percli lint` are deferred to
`make verify-integration` with their exact commands. `buf` likewise
(the CI `proto-compat` job already runs it).

**D-005 · Verified pins** (looked up 2026-11-03, never invented):
esbuild `0.25.12` (latest 0.25.x) · fastembed `0.9.1` (Apache-2.0) ·
prometheus-client `0.26.0` (Apache-2.0 AND BSD-2-Clause) ·
`jaegertracing/jaeger:2.22.0` · `prom/prometheus:v3.15.1` ·
`persesdev/perses:v0.55.1` · `ghcr.io/astral-sh/uv:0.11` ·
`gcr.io/distroless/nodejs22-debian12:nonroot` · `node:22-slim` ·
ruff-pre-commit `v0.17.3` · uv-pre-commit `0.11.32` ·
pre-commit-hooks `v6.0.0`. Confirmed fact §3: `headroom-ai==0.31.0`
requires `litellm>=1.86.2` while `aider-chat==0.86.2` pins
`litellm==1.81.10`; aider ships `tree-sitter-language-pack==0.13.0`.

**D-006 · Ingest image contents (L0.2).** `ingest/` imports
`agent.openbao` (secret leasing) and `agent.router` (optional RouteLLM
tier proposal), both lazily, plus the generated protobuf package — but not
`agent.lessons`. `Containerfile.ingest` therefore copies
`agent/__init__.py`, `agent/openbao.py`, `agent/router.py` and
`agent/gen/`; omitting the first two would crash the issue sync and every
chat loop at runtime. `requirements-ingest.txt` is written by hand now and
becomes a generated artifact in L1.1.

**D-007 · `make ingest-container`.** Builds the ingest image through the
compose `chat` profile and runs one Forgejo issue-sync pass inside it, so
the target exercises the image rather than only building it.

**D-008 · Sync Valkey client for the hand-off store (L0.3).** The prompt
says to reuse `agent.budget.client()`, but that is the `valkey.asyncio`
client, while `put_handoff`/`get_handoff` and the `python -m
agent.verifier` CLI are synchronous: driving the async client there would
need `asyncio.run` per call, binds the cached singleton to a dead loop on
the second call, and fails outright inside a running loop. So
`agent/budget.py` gains `sync_client()` — a blocking client on the same
`VALKEY_URL` (2 s connect / 5 s socket timeouts so an unreachable Valkey
degrades fast at import). One source of Valkey configuration is kept; the
TTL reuses `budget.MAX_TASK_WALL_S`.
