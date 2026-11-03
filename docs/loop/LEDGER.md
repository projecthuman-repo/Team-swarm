# Lean & Precise — ledger

Every file or runnable option a task touched. Nothing is ever `removed`:
an item whose source of truth moves is `moved` (and the old location says
where it went) or `generated` (the file stays, with a header saying so).

| item | status (kept / extended / generated / moved) | where |
|---|---|---|
| `scripts/footprint_wheels.py` | generated (new) | L0.1 — resolves every set via `uv pip compile`, sums PyPI wheel sizes |
| `scripts/footprint_report.sh` | generated (new) | L0.1 — wheels + image sizes + `system df` + SLA latency |
| `docs/footprint-baseline.md` | generated (new) | L0.1 — first run of `make footprint` |
| `make footprint` | extended (new target) | Makefile |
| `make verify-integration` | extended (new target) | Makefile — single home for every deferred check |
| `docs/loop/{STATE,LEDGER,DECISIONS}.md` | generated (new) | loop bookkeeping |
| `headroom-ai==0.31.0` | moved | L0.2 — from `requirements-elevation.txt` to `requirements-ingest.txt` (ingest image) and the isolated uv tool env (`make tools`) |
| `requirements-elevation.txt` | kept | L0.2 — keeps litellm, nvidia-ml-py, psutil; header says where headroom-ai moved |
| `requirements-ingest.txt` | generated (new) | L0.2 — ingest image runtime set |
| `deploy/Containerfile.ingest` | generated (new) | L0.2 — chat ingress + issue sync image; no git/aider |
| `chat-ingress` compose service | extended | L0.2 — builds `Containerfile.ingest` |
| `make ingest-container` / `make tools` | extended (new targets) | Makefile |
| `scripts/morning_headroom_learn.py` | extended | L0.2 — `HEADROOM_BIN` (default `headroom`, uv-tool path) |
| `HEADROOM_BIN` | extended (new env var) | `.env.example` |
| `agent/shared_context.py` | extended | L0.3 — Valkey `handoff:<task_id>` (TTL `MAX_TASK_WALL_S`); in-memory dict only when `VALKEY_URL` unset/unreachable |
| Headroom `SharedContext` compression | moved | L0.3 — compression now happens in flight at the Headroom proxy (`VERIFIER_API_BASE` defaults to `HEADROOM_URL` when `HEADROOM_ENABLED=1`) |
| `agent/budget.py` | extended | L0.3 — `sync_client()` beside `client()` on the same `VALKEY_URL` |
| `agent/verifier.py` | extended | L0.3 — `VERIFIER_API_BASE` default resolution |
| `VERIFIER_API_BASE` | extended (env var documented) | `.env.example` |
| `conventions/context.md` hand-off row | extended | L0.3 — "Valkey hand-off store, compressed in flight by the Headroom proxy" |
| `tests/test_handoff.py` | generated (new) | L0.3 — cross-process acceptance tests |
| `deploy/Containerfile.relay` | extended | L0.4 — copies `package-lock.json`, `npm ci`, ships `relay.ts` + `relay_cdc.ts` |
| CDC relay option (`relay_cdc.ts` in-container) | extended | L0.4 — now runnable via the compose `command` comment |
