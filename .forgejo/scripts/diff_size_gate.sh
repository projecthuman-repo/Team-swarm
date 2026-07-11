#!/usr/bin/env bash
# Diff-size gate: agent PRs must stay reviewable. Blocks merges whose diff
# against the target branch exceeds MAX_CHANGED_LINES (default 1500).
set -euo pipefail

BASE="${BASE_BRANCH:-integration}"
MAX="${MAX_CHANGED_LINES:-1500}"

git fetch origin "$BASE" --depth=50 >/dev/null 2>&1 || true
CHANGED=$(git diff --shortstat "origin/$BASE"...HEAD 2>/dev/null \
  | awk '{ins=0; del=0; for(i=1;i<=NF;i++){if($(i+1)~/insertion/)ins=$i; if($(i+1)~/deletion/)del=$i} print ins+del}')
CHANGED="${CHANGED:-0}"

echo "diff size vs $BASE: $CHANGED changed line(s) (limit $MAX)"
if [ "$CHANGED" -gt "$MAX" ]; then
  echo "DIFF-SIZE GATE FAILED: split this change into smaller tasks"
  exit 1
fi
