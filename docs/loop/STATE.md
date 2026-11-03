# Lean & Precise (v4.1 → v4.1.1) — loop state

Resume: re-paste the build-loop prompt; it continues from the first row
whose status is not `DONE` / `DONE-DEFERRED-INTEGRATION`.

## Environment (recorded on first run, 2026-11-03)

| item | value |
|---|---|
| base branch | `main` (no `integration` branch exists) |
| working branch | `feature/lean-precise` |
| `uv --version` | `uv 0.11.32` → Containerfiles use `ghcr.io/astral-sh/uv:0.11` |
| Python | 3.12.13 via `uv python install 3.12` (`UV_PYTHON=3.12`) |
| node / npm | v22.23.4 / 10.9.9 |
| container runtime | docker CLI present, daemon **not running**; podman absent → image builds deferred |
| psql | 16.15 (client only; no server running) |
| syft | absent — remote installer blocked by session policy → license-gate runs deferred |
| percli | absent (Go release binary, same policy) → `percli lint` deferred |
| buf | absent → `buf lint` deferred (CI `proto-compat` job runs it) |
| pre-commit | 4.7.0 via `uv tool install pre-commit` |

## Tasks

| id | task | status | evidence / deferred command |
|---|---|---|---|
| L0.1 | Baseline + verify-integration target | DONE | `make footprint` printed the wheel table in-session (routing 3,213.9 MB, embeddings 3,083.6 MB, agent 131.2 MB/113 pkgs — matches §3); committed as `docs/footprint-baseline.md`. Image/disk/SLA lines run in `make verify-integration`. |
| L0.2 | Headroom isolation | DONE | `uv pip compile` (py3.12/manylinux_2_28): agent set `requirements.txt`+`requirements-elevation.txt` resolves (114 pins, aider-chat 0.86.2, litellm 1.81.10, no headroom-ai); `requirements-ingest.txt` resolves (68 pins, headroom-ai 0.31.0, litellm 1.106.6); the old aider+headroom-ai co-install is unsatisfiable. `make tools` installed `headroom, version 0.31.0` at `~/.local/bin/headroom`; agent env cannot `import headroom`. `test_learn_*` 3/3 pass. Image build deferred: `make ingest-container` (in `verify-integration` via L1.4). |
| L0.3 | Cross-process hand-off via Valkey | DONE | `tests/test_handoff.py` (5 tests): a bundle written by one interpreter through the real valkey-py client into an in-test RESP server is read by a fresh `python -m agent.verifier task-42`, which returns PASS from all three lenses (the fake model passes only when the bundle reached the request); missing bundle reported; unreachable Valkey degrades to memory with a warning; `VERIFIER_API_BASE` defaults verified. Existing verifier tests pass. |
| L0.4 | Relay build hygiene | DONE | Every `COPY` source exists; `npm ci --omit=optional` on a clean copy of exactly the build context installed 24 packages; `tsc --noEmit` passes for `relay.ts` + `relay_cdc.ts`. Image build runs in `verify-integration` (L1.4). |
| L1.1 | uv is the source of truth | DONE-DEFERRED-INTEGRATION | `uv lock` resolves 344 pkgs; `uv sync --frozen --group agent --group dev` installs; `uv lock --check` passes; a second `make lock` changes nothing (checksums); `uv sync --group agent --group ingest` → `error: Groups agent and ingest are incompatible`; agent closure has 0 torch (`uv export --no-dev --group agent`, D-014); ruff + 73 tests pass in the synced env. **Deferred:** `make torch-cpu-lock` (needs download.pytorch.org — D-011). |
| L1.2 | Locked CI with cache | DONE | `make ci-local` passed end to end in-session: locked sync + `uv lock --check`, generated files match a fresh `make lock`, ruff, pytest (73, coverage 28.5% ≥ 20%), bandit medium+, diff-size, headroom gate in an isolated venv (PASS), license gate DEFERRED (no syft — in `verify-integration`). `proto-compat` job unchanged. Found two pre-existing gate defects on the way (bandit B310 → fixed in the Phase-0 fix commit; diff-size crash → D-013). |
| L1.3 | pre-commit for developer machines | DONE | `pre-commit run --all-files`: pass 1 reformatted 29 files (all 28 changed `.py` files AST-identical to before — formatting only) and added a final newline to `.forgejo/fixtures/tool_output.json`; pass 2 exit 0 on every hook. 73 tests pass; headroom gate PASS and its negative test still fails. `make hooks` installed `.git/hooks/pre-commit`; `make ci-local` (with the new pre-commit step) passes. |
| L1.4 | `.dockerignore` and image split | DONE-DEFERRED-INTEGRATION | `npm run build` produced `dist/relay.js` (720 KB) + `dist/relay_cdc.js` (848 KB); both load with **no node_modules** and fail only at the NATS connect. Builder stages simulated locally with the exact uv flags + strip: agent venv 507 MB apparent, imports agent/ingest modules and runs `aider 0.86.2`; ingest venv 236 MB with the four skipped packages, imports ingest with only the copied files. `tests/test_containerfiles.py` (6) checks keywords, COPY sources vs `.dockerignore`, `--from` stages, relay CMD vs esbuild outputs. **Deferred:** `./scripts/image_sizes.sh` in `verify-integration`. **Forecast:** agent ≈ 670 MB (≤ 700 ✓); ingest venv alone 236 MB → image likely OVER 300 MB (D-017); relay ≈ distroless base + 1.6 MB. |
| L1.5 | Lite spine and disk hygiene | DONE-DEFERRED-INTEGRATION | `deploy/compose.lite.yaml` parses and each of its four services is identical to its `deploy/compose.yaml` definition (networks + volumes too) — `tests/test_compose_lite.py` (4). `bootstrap.sh --dry-run` exits 0 in lite mode (no apicurio/seaweedfs → "would SKIP Protobuf schema registration") and in full mode, with fake runtimes. `make -n` shows `up-lite`/`down-lite`/`ollama`/`clean`/`disk-usage`; `forgejo` now names its service (D-020). README "Lite path" block. **Deferred:** `./scripts/lite_smoke.sh` in `verify-integration` (boot lite, bootstrap, ollama+forgejo+relay+agent, inject one task, assert `done`). |
| L1.6 | Sandbox workspace policy | DONE | `tests/test_sandbox_workspace.py` (6): an aged fake `task-*` orphan (mtime 1801 s > 1800) is removed while a fresh one, a non-task dir and a symlinked `task-link` target survive; defaults come from `SANDBOX_WORKDIR`/`TASK_TIMEOUT_S`; a heartbeat keeps a live dir; `agent_worker.main` reaps before connecting; compose tmpfs `/tmp:size=${SANDBOX_TMPFS_SIZE:-4g}` + `/home/swarm:size=1g`; k8s emptyDir `tmp` 4Gi / `home` 1Gi in every agent Deployment. 89 tests pass. |
| L2.1 | Two-stage quality gate | DONE | `tests/test_quality_loop.py` (+5, real git repos + real ruff/pytest through a recording `_run` spy): a syntax error yields a stage-1 red whose output is the `ruff check app.py` run only, the repair prompt carries it, and the recorded command list has **no pytest**; a green change runs `ruff check test_app.py` then `ruff check .` + `pytest -q`, both timings ints; no `.py` change skips stage 1; `verify_cmd` runs in stage 2. `tests/test_gate_timing.py` (3): `handle_task` writes `gate_stage1_ms/gate_stage2_ms`; migration + schema carry the columns; SLA line. **Real Postgres 16 + pgvector 0.6 in-session:** `db/schema.sql` + migrations 002–006 apply cleanly and `scripts/sla_report.py` printed `gate median s1 / s2 : 1.5s / 40.0s` over synthetic rows. 97 tests pass. |
| L2.2 | Hybrid retrieval | DONE | `tests/test_lessons.py` (+4): with an embedding, `search_lessons` calls `match_lessons_hybrid($1, $2, $3, $4)` with the tag; without one, only `fts @@ websearch_to_tsquery('simple', $1)` (no `<=>`); a Qdrant mirror keeps its dense path untagged and tagged queries go to Postgres. **Pure-SQL test ran in-session on real Postgres 16 + pgvector 0.6** (new `tests/conftest.py` `pg` fixture, throwaway schema with `db/schema.sql` applied): dense alone ranks the "near" lesson first, the hybrid query ranks the exact `parse_outbox_row` lesson first and respects the tag; the text-only path returns it alone. 007 applies twice cleanly (idempotent) and via `db/schema.sql`. 101 tests pass. Also in `verify-integration` with `REQUIRE_POSTGRES=1` (skip → failure) against the spine image. |
| L2.3 | fastembed default | DONE-DEFERRED-INTEGRATION | `EMBEDDING_BACKEND` (fastembed default, sentence-transformers kept) with `BAAI/bge-small-en-v1.5`/384; bge-m3 served through fastembed from its ONNX export. `tests/test_embeddings.py` (7): defaults per backend (fresh interpreter); fastembed path returns a normalized 384-vector (stubbed model); **without fastembed → `embed()` is None, text-only as before**; ST path intact; unknown backend errors; bge-m3 registers through the **real installed fastembed 0.9.1 API** (dim 1024, idempotent); `shadow_eval --require-not-worse` exits 1 only when the candidate is worse. Torch-free check `scripts/check_torch_free.sh` → 0 torch entries for `embeddings` (negative control: 16 for `embeddings-torch`), now a CI + `ci-local` step. README options rows + RUNBOOK §2 (mandatory `--reembed`) + `.env.example`. **Deferred:** real `embed("hello")` → 384 (HuggingFace is 403 by this session's egress policy — the model cannot download here) and `scripts/embeddings_shadow.sh` (re-embed + golden pack per embedder; fastembed green rate ≥ baseline) in `verify-integration`. |
| L2.4 | Multi-language incremental signature index | TODO | |
| L2.5 | Storage hygiene in the morning routine | TODO | |
| L3.1 | Three-zone license gate | TODO | |
| L3.2 | Jaeger + Prometheus + Perses | TODO | |
| L3.3 | Golden pack to ten tasks | TODO | |
| L3.4 | Docs and runbook | TODO | |

## Phase-0 review (adversarial, 13 agents: 4 finder dimensions, 1 skeptic per finding)

9 findings, 5 confirmed and fixed in `lean(L0.x): fix …`: wheel-report failures did not fail
`make footprint`/`verify-integration`; the CDC `command` comment sat inside the relay's
`environment:` map; the SLA filter dropped the `publish->claim` line; the image builds described as
deferred were not in `verify-integration`. Also fixed: bandit B310 in `footprint_wheels.py`
(found by `make ci-local`, missed by the review) and plain `npm ci` in the relay image (refuted as
a defect, adopted because the spec says `npm ci`). 4 refuted (scope/egress claims).

## Deferred commands by phase

### Phase 0 — closed 2026-11-03 (tag `lean-phase-0`)

Unit protocol green: `ruff check .`, `pytest -q` (73 passed, Python 3.12.13). Deferred to the
runtime host, all inside `make verify-integration`:

- `make footprint` with a running container runtime (image sizes, `system df`, SLA latency lines)
- `$(RUNTIME) build -f deploy/Containerfile.ingest -t swarm-ingest:dev .` (L0.2)
- `$(RUNTIME) build -f deploy/Containerfile.relay -t swarm-relay:dev .` (L0.4)
- license gate: needs syft (`uv run --no-sync python .forgejo/scripts/license_gate.py`)
