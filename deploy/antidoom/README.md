# Antidoom — offline model improvement (v4.2 Workstream 9)

**Offline, evidence-gated, never on the critical path.** Antidoom
(`Liquid4All/antidoom`, Apache-2.0) trains a LoRA adapter via Final Token
Preference Optimization (FTPO) — adapted from Antislop
(arXiv:2510.15061) — to suppress runaway repetition ("doom loops") in
reasoning models. Reasoning-heavy coders like Ornith are exactly the class
prone to it, and a looping agent is the token-snowball failure mode the
runtime already guards (T5.3). Antidoom attacks it at the model level.

This directory runs on a **separate GPU host, outside the swarm's runtime
path**. It never blocks the runtime swarm (Phase 5, offline track).

## Scope

- Targets the self-hosted **Ornith-1.0-9B / 35B** checkpoints only. 397B
  is out of scope for local LoRA.
- **Do not pull LFM2 checkpoints** — target Ornith only.
- Experimental (upstream is 2 commits, no releases) and can degrade a
  model if overtrained. Promotion is gated on measured improvement (T9.4).

## Steps (T9.1 → T9.5)

1. **T9.1 Environment.** `docker compose -f deploy/antidoom/compose.yaml up`
   on a GPU host. NVIDIA/CUDA is the default; for AMD/ROCm (MI-series) use
   the upstream `configs/default_amd.yaml` with
   `attention_backend: TRITON_ATTN`. Verify:
   `uv run antidoom -c configs/default.yaml --model-name <ornith-9b-hf-id>`
   starts generation.
2. **T9.2 Baseline.** `python scripts/doomloop_probe.py --transcripts <dir>
   --snowball-rate` records the current doom-loop + snowball rate for 9B
   and 35B.
3. **T9.3 Train.** Generate ≥15k FTPO preference rows from a
   *swarm-relevant* coding/reasoning prompt set (NOT the LFM2 default),
   train the LoRA (`lora_r` 128–256, `lr` 1e-5–2e-5,
   `early_stopping_chosen_win≈0.4`), and merge. `./train.sh` wraps this.
4. **T9.4 Serve + shadow-eval.** Serve the merged model behind a distinct
   Ollama/vLLM tag; run `make shadow-eval` (T6.1/T7.6) plus the doom-loop
   probe against stock Ornith. **Promote only if** doom-loop rate AND
   tokens-per-PR drop with **no green-rate regression**. No improvement →
   no promotion.
5. **T9.5 License.** The merged checkpoint is an **MIT derivative** of
   Ornith (MIT permits it) and must pass the license-gate metadata check.
   Verify the FTPO dataset license at pin time — prefer an in-house prompt
   set; flag `LiquidAI/antidoom-mix-v1.0` if used. Record in the ledger.

## Why it's safe

Off the runtime path, GPU-only, and promoted solely on evidence. If it
regresses anything, it is simply not promoted — stock Ornith keeps
serving. The runtime circuit breaker (T5.3) remains the always-on guard
regardless of any adapter.
