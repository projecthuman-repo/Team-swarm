#!/usr/bin/env bash
# Enable auto-merge on a PR: merges feature/* -> integration only when all
# checks pass (runbook §11 step 4). Auto-merge to main is NEVER configured
# (Hard Rule 5).
#
#   ./scripts/forgejo_automerge.sh <pr-number>
set -euo pipefail

FORGEJO="${FORGEJO_URL:-http://localhost:3000}"
OWNER="${FORGEJO_OWNER:-swarm}"
REPO="${FORGEJO_REPO:-sandbox-repo}"
PR="${1:?usage: forgejo_automerge.sh <pr-number>}"
: "${FORGEJO_TOKEN:?set FORGEJO_TOKEN (bot token from OpenBao)}"

BASE=$(curl -sf "$FORGEJO/api/v1/repos/$OWNER/$REPO/pulls/$PR" \
  -H "Authorization: token $FORGEJO_TOKEN" | jq -r .base.ref)
if [ "$BASE" != "integration" ]; then
  echo "REFUSED: PR #$PR targets '$BASE' — auto-merge is integration-only" >&2
  exit 1
fi

curl -sf -X POST "$FORGEJO/api/v1/repos/$OWNER/$REPO/pulls/$PR/merge" \
  -H "Authorization: token $FORGEJO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"Do":"merge","merge_when_checks_succeed":true}'
echo "auto-merge armed for PR #$PR (merges when checks pass)"
