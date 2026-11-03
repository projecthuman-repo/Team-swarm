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
