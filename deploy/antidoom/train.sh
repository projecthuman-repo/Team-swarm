#!/usr/bin/env bash
# Antidoom FTPO adapter training + merge (v4.2 T9.3) — GPU host, offline.
# Wraps the upstream antidoom runner with swarm-relevant defaults.
#
#   ORNITH_HF_ID=deepreinforce-ai/Ornith-1.0-9B ./deploy/antidoom/train.sh
set -euo pipefail

ORNITH_HF_ID="${ORNITH_HF_ID:-deepreinforce-ai/Ornith-1.0-9B}"
LORA_R="${LORA_R:-128}"
LR="${LR:-1.5e-5}"
FTPO_ROWS="${FTPO_ROWS:-15000}"
RUN_DIR="${RUN_DIR:-runs/antidoom-$(date +%Y%m%d 2>/dev/null || echo run)}"
CONFIG="${ANTIDOOM_CONFIG:-configs/default.yaml}"

case "$ORNITH_HF_ID" in
  *LFM2*|*lfm2*)
    echo "REFUSED: target Ornith only, never LFM2 checkpoints (T9.3)." >&2
    exit 1;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not installed. pip install uv (see deploy/antidoom/README.md)." >&2
  exit 2
fi

echo "==> generating >= ${FTPO_ROWS} FTPO preference rows (coding/reasoning set)"
echo "==> training LoRA r=${LORA_R} lr=${LR} on ${ORNITH_HF_ID}"
uv run antidoom -c "$CONFIG" \
  --model-name "$ORNITH_HF_ID" \
  --lora-r "$LORA_R" \
  --learning-rate "$LR" \
  --num-preference-rows "$FTPO_ROWS" \
  --early-stopping-chosen-win 0.4 \
  --output-dir "$RUN_DIR" \
  --merge

echo
echo "Merged anti-doom checkpoint under $RUN_DIR."
echo "Next (T9.4): serve behind a distinct tag, then:"
echo "  make shadow-eval CANDIDATE=AIDER_MODEL=ollama/<anti-doom-tag>"
echo "  python scripts/doomloop_probe.py --transcripts <new-run-transcripts>"
echo "Promote ONLY if doom-loop rate + tokens/PR drop with no green-rate"
echo "regression (T9.4). The merged model is an MIT derivative — run the"
echo "license-gate metadata check and record the dataset license (T9.5)."
