#!/usr/bin/env bash
# L2.3 / R4 acceptance on the runtime host (`make verify-integration`):
# first-pass green rate of the golden pack per embedder, against the v4.1
# baseline (sentence-transformers all-MiniLM-L6-v2):
#   api-all-minilm       the default since R4 — Ollama's /v1/embeddings
#   fastembed-bge-small  the in-process option (L2.3)
# Each config re-embeds the lessons first (mandatory on any embedder
# switch), then runs the golden pack with the retrieval context pack on
# (CONTEXT_PACK=1). Both must be at least as green as the baseline. The
# default's comparison runs last, so eval/last_report.md is its report.
# Needs the spine's Ollama with the embedding model (`make models`).
set -euo pipefail
cd "$(dirname "$0")/.."
export CONTEXT_PACK=1
API_BASE="${EMBEDDING_API_BASE:-http://localhost:11434/v1}"

run_cfg() { # $1 = label, $2 = uv extra ("-" = none), rest = env assignments
  local label="$1" extra=()
  [ "$2" = "-" ] || extra=(--extra "$2")
  shift 2
  env "$@" uv run --frozen --group agent "${extra[@]}" python scripts/seed_knowledge.py --reembed
  env "$@" uv run --frozen --group agent "${extra[@]}" python scripts/shadow_eval.py --single "$label"
}

env EMBEDDING_BACKEND=api EMBEDDING_API_BASE="$API_BASE" EMBEDDING_MODEL=all-minilm \
  uv run --frozen --group agent python scripts/seed_knowledge.py --index-repo .
run_cfg baseline-st-minilm embeddings-torch \
  EMBEDDING_BACKEND=sentence-transformers EMBEDDING_MODEL=all-MiniLM-L6-v2 EMBEDDING_DIM=384
run_cfg fastembed-bge-small embeddings \
  EMBEDDING_BACKEND=fastembed EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 EMBEDDING_DIM=384
run_cfg api-all-minilm - \
  EMBEDDING_BACKEND=api EMBEDDING_API_BASE="$API_BASE" EMBEDDING_MODEL=all-minilm EMBEDDING_DIM=384

fail=0
uv run --no-sync python scripts/shadow_eval.py --require-not-worse \
  --report eval/baseline-st-minilm.json eval/fastembed-bge-small.json || fail=1
uv run --no-sync python scripts/shadow_eval.py --require-not-worse \
  --report eval/baseline-st-minilm.json eval/api-all-minilm.json || fail=1
exit "$fail"
