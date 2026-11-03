#!/usr/bin/env bash
# L2.3 acceptance on the runtime host (`make verify-integration`):
# first-pass green rate with the fastembed default >= the v4.1 baseline
# embedder (sentence-transformers all-MiniLM-L6-v2). Each config re-embeds
# the lessons first (mandatory on any embedder switch), then runs the golden
# pack with the retrieval context pack on (CONTEXT_PACK=1).
set -euo pipefail
cd "$(dirname "$0")/.."
export CONTEXT_PACK=1

run_cfg() { # $1 = label, $2 = extra, rest = env assignments
  local label="$1" extra="$2"
  shift 2
  env "$@" uv run --frozen --group agent --extra "$extra" python scripts/seed_knowledge.py --reembed
  env "$@" uv run --frozen --group agent --extra "$extra" python scripts/shadow_eval.py --single "$label"
}

uv run --frozen --group agent --extra embeddings python scripts/seed_knowledge.py --index-repo .
run_cfg baseline-st-minilm embeddings-torch \
  EMBEDDING_BACKEND=sentence-transformers EMBEDDING_MODEL=all-MiniLM-L6-v2 EMBEDDING_DIM=384
run_cfg fastembed-bge-small embeddings \
  EMBEDDING_BACKEND=fastembed EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 EMBEDDING_DIM=384
uv run --no-sync python scripts/shadow_eval.py --require-not-worse \
  --report eval/baseline-st-minilm.json eval/fastembed-bge-small.json
