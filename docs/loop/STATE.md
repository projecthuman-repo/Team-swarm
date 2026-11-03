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
| L0.2 | Headroom isolation | TODO | |
| L0.3 | Cross-process hand-off via Valkey | TODO | |
| L0.4 | Relay build hygiene | TODO | |
| L1.1 | uv is the source of truth | TODO | |
| L1.2 | Locked CI with cache | TODO | |
| L1.3 | pre-commit for developer machines | TODO | |
| L1.4 | `.dockerignore` and image split | TODO | |
| L1.5 | Lite spine and disk hygiene | TODO | |
| L1.6 | Sandbox workspace policy | TODO | |
| L2.1 | Two-stage gate | TODO | |
| L2.2 | Hybrid retrieval | TODO | |
| L2.3 | fastembed default | TODO | |
| L2.4 | Multi-language incremental signature index | TODO | |
| L2.5 | Storage hygiene in the morning routine | TODO | |
| L3.1 | Three-zone license gate | TODO | |
| L3.2 | Jaeger + Prometheus + Perses | TODO | |
| L3.3 | Golden pack to ten tasks | TODO | |
| L3.4 | Docs and runbook | TODO | |

## Deferred commands by phase

(filled at each phase close-out)
