#!/usr/bin/env bash
# Diff-size gate: agent PRs must stay reviewable. Blocks merges whose diff
# against the target branch exceeds MAX_CHANGED_LINES (default 1500).
#
# Base branch: BASE_BRANCH if set, else the PR's base ref, else
# `integration` when it exists, else `main` (Lean & Precise §0 rule).
# Enforced on the agent auto-merge path (base `integration`, Hard Rule 5)
# or with DIFF_SIZE_ENFORCE=1; PRs into `main` — the human gate — get the
# size reported but not blocked (DECISIONS D-013).
#
# Generated artifacts are not counted (they are regenerated, not authored);
# CI separately proves they match a fresh `make lock`.
set -euo pipefail

MAX="${MAX_CHANGED_LINES:-1500}"

if [ -n "${BASE_BRANCH:-}" ]; then
  BASE="$BASE_BRANCH"
elif [ -n "${GITHUB_BASE_REF:-}" ]; then
  BASE="$GITHUB_BASE_REF"
elif git ls-remote --exit-code --heads origin integration >/dev/null 2>&1; then
  BASE=integration
else
  BASE=main
fi

git fetch origin "$BASE" --depth=50 >/dev/null 2>&1 || true
if ! git rev-parse --verify --quiet "origin/$BASE" >/dev/null; then
  echo "DIFF-SIZE GATE ERROR: origin/$BASE not found — cannot measure the diff (fail closed)"
  exit 2
fi

STAT="$(git diff --shortstat "origin/$BASE"...HEAD -- . \
  ':(exclude)uv.lock' ':(exclude)relay/package-lock.json' \
  ':(exclude)requirements*.txt' ':(exclude)agent/gen/**')"
CHANGED="$(echo "$STAT" | awk '{ins=0; del=0; for(i=1;i<=NF;i++){if($(i+1)~/insertion/)ins=$i; if($(i+1)~/deletion/)del=$i} print ins+del}')"
CHANGED="${CHANGED:-0}"

echo "diff size vs $BASE: $CHANGED changed line(s), generated files excluded (limit $MAX)"
if [ "$CHANGED" -le "$MAX" ]; then
  exit 0
fi
if [ "$BASE" = "integration" ] || [ "${DIFF_SIZE_ENFORCE:-0}" = "1" ]; then
  echo "DIFF-SIZE GATE FAILED: split this change into smaller tasks"
  exit 1
fi
echo "over the limit, but $BASE is a human-reviewed gate: reported, not enforced (set DIFF_SIZE_ENFORCE=1 to enforce)"
