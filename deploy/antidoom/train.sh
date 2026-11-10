#!/usr/bin/env bash
# Antidoom FTPO adapter training + merge (v4.2 T9.3) — GPU host, offline.
# Wraps the upstream antidoom runner with swarm-relevant defaults.
#
#   ORNITH_HF_ID=deepreinforce-ai/Ornith-1.0-9B ./deploy/antidoom/train.sh
#
# Checked against upstream's CLI and configs/default.yaml (Liquid4All/antidoom,
# main, 2026-11-10; review R7): the runner has flags for the model, the number
# of preference pairs and the output directory, while the LoRA rank, the
# learning rate, the early-stopping threshold and the merge step are fields
# of the config file. So this script writes a run config derived from
# $ANTIDOOM_CONFIG with LORA_R and LR set, and passes only flags that exist.
# early_stopping_chosen_win (0.4) and merge_lora (true) are upstream defaults.
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
for key in lora_r learning_rate early_stopping_chosen_win merge_lora; do
  grep -qE "^ *${key}:" "$CONFIG" || {
    echo "config $CONFIG has no '${key}:' field — check it against upstream's default.yaml." >&2
    exit 2
  }
done

mkdir -p "$RUN_DIR"
RUN_CONFIG="$RUN_DIR/antidoom-config.yaml"
sed -E \
  -e "s/^( *lora_r:).*/\1 ${LORA_R}/" \
  -e "s/^( *learning_rate:).*/\1 ${LR}/" \
  "$CONFIG" > "$RUN_CONFIG"

echo "==> generating >= ${FTPO_ROWS} FTPO preference rows (coding/reasoning set)"
echo "==> training LoRA r=${LORA_R} lr=${LR} on ${ORNITH_HF_ID} (config: $RUN_CONFIG)"
uv run antidoom -c "$RUN_CONFIG" \
  --model-name "$ORNITH_HF_ID" \
  --target-pairs "$FTPO_ROWS" \
  --output-dir "$RUN_DIR"

echo
echo "Merged anti-doom checkpoint under $RUN_DIR."
echo "Next (T9.4): serve behind a distinct tag, then:"
echo "  make shadow-eval CANDIDATE=AIDER_MODEL=ollama/<anti-doom-tag>"
echo "  python scripts/doomloop_probe.py --transcripts <new-run-transcripts>"
echo "Promote ONLY if doom-loop rate + tokens/PR drop with no green-rate"
echo "regression (T9.4). The merged model is an MIT derivative — run the"
echo "license-gate metadata check and record the dataset license (T9.5)."
