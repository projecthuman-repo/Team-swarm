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

**D-009 · `requires-python = ">=3.12,<3.13"` (L1.1).** With `>=3.12` the
universal lock is unsolvable: for Python ≥3.14 aider-chat pins
`numpy==1.26.4` while fastembed needs `numpy>=2.3`. Every image and CI job
runs 3.12, so the lock is scoped to it.

**D-010 · Ingest group carries its whole runtime (L1.1 vs L1.4).** L1.1
lists `ingest = ["headroom-ai==0.31.0"]`, but L1.4 builds the ingest image
with `uv sync --only-group ingest`, which excludes the base dependencies —
the image would lack asyncpg/requests/protobuf/valkey and crash. The group
therefore lists L0.2's set: asyncpg, requests, protobuf, valkey,
headroom-ai==0.31.0.

**D-011 · CPU-torch source shipped disabled (L1.1).** `download.pytorch.org`
is blocked by this session's network proxy (CONNECT 403), so with the torch
source active `uv lock` cannot run here, and without a lock L1.2's CI and
L1.4's images have nothing to install. pyproject defines the `pytorch-cpu`
index and keeps the `[tool.uv.sources] torch` mapping in a marked,
commented block; `make torch-cpu-lock` enables it, re-locks, and fails
unless both torch extras resolve `torch==…+cpu` with no `nvidia-*` wheels.
It runs in `make verify-integration` on the owner's host (whose allow-list
gains download.pytorch.org, §9). The default embeddings path (fastembed)
needs no torch at all. Also: the prompt's mapping alone would never have
applied — uv sources bind only direct dependencies and torch was
transitive — so `torch` is now listed directly in `embeddings-torch` and
`routing`.

**D-012 · `--only-extra` emulated (L1.1).** uv 0.11 has no
`uv export --only-extra`. `scripts/lock_export.sh` exports base+extra and
drops every package the base closure already pins (by name), so each
extras file stays an add-on to `requirements.txt` with pins from the same
lock. Requirements files are exported without hashes (uv.lock is the
hash-verified source; the files are the pip fallback, as before).

**D-014 · Torch-free checks use `uv export`, not `uv tree` (L1.1, L2.3).**
`uv tree` in uv 0.11 has no `--extra` flag and always renders every extra
(annotated `(extra: …)`), so `uv tree --group agent | grep torch` matches
torch through the torch extras even though the agent closure has none. The
selection-aware equivalent is `uv export --frozen --no-dev --group agent`
(0 torch lines); L2.3's CI check uses `uv export --extra embeddings`.

**D-015 · Tags are local (§7).** The session's git proxy accepts the
working-branch push but refuses tag refs (`send-pack: unexpected
disconnect`, three attempts with backoff). Phase tags are created locally
and listed in `docs/loop/OWNER_STOP.md` for a push from a machine with
normal access.

**D-013 · Diff-size gate: base, scope, generated files (L1.2).** Running the
real gates (`make ci-local`) showed `.forgejo/scripts/diff_size_gate.sh` has
crashed on every run since v4.0: it diffed against `origin/integration`,
which does not exist here, and `set -euo pipefail` aborted on the failing
`git diff` inside a command substitution (exit 128) — so the CI `gates`
job could never pass. Fixed: the base is `BASE_BRANCH`, else the PR's base
ref (`GITHUB_BASE_REF`), else `integration` if it exists, else `main` (the
§0 rule); an unmeasurable diff now fails closed with exit 2 and a message.
Generated artifacts (`uv.lock`, `relay/package-lock.json`,
`requirements*.txt`, `agent/gen/`) are not counted — a dependency bump
would otherwise block every agent PR — and CI separately proves they equal
a fresh `make lock`, so they cannot hide hand edits. **Scope (owner may
veto):** the limit is enforced on the agent auto-merge path (base
`integration`, Hard Rule 5) or with `DIFF_SIZE_ENFORCE=1`; for PRs into
`main` — the human gate — the size is printed but does not block. The
script's own header defines it as the agent-PR gate and its original
default base was `integration`; no agent path is weakened, because agents
cannot merge into `main`. Without this, a multi-task owner PR such as this
loop's could never pass `gates`.

**D-016 · pre-commit details (L1.3).** Hook revs are the verified tags in
D-005; `ruff` is the id the spec names (still present at v0.17.3, same
entry as `ruff-check`, both `--force-exclude`). Top-level
`exclude: ^agent/gen/` keeps the hygiene fixers off generated protobuf code.
`trailing-whitespace` keeps Markdown hard breaks
(`--markdown-linebreak-ext=md`). `check-added-large-files --maxkb=512`
only inspects newly *added* files, so the existing 0.7 MB `uv.lock` is
unaffected. pre-commit joined the `dev` group (MIT), so CI and
`ci-local` run it from the locked env (`uv run --no-sync`). Hook repos are
fetched from github.com, so the CI runner allow-list in OWNER_STOP gains
`github.com`.
