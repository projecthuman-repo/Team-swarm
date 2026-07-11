#!/usr/bin/env bash
# Forgejo repo wiring (runbook §11): labels, branch protection, and the
# integration branch. Run once per target repo with an ADMIN token:
#
#   FORGEJO_TOKEN=<admin token> ./scripts/forgejo_setup.sh
#
# The BOT account token (write scope, stored in OpenBao) is separate —
# branch protection below restricts it to feature/*.
set -euo pipefail

FORGEJO="${FORGEJO_URL:-http://localhost:3000}"
OWNER="${FORGEJO_OWNER:-swarm}"
REPO="${FORGEJO_REPO:-sandbox-repo}"
BOT="${FORGEJO_BOT_USER:-swarm-bot}"
: "${FORGEJO_TOKEN:?set FORGEJO_TOKEN to an admin token}"

api() {
  local method="$1" path="$2" body="${3:-}"
  curl -sf -X "$method" "$FORGEJO/api/v1$path" \
    -H "Authorization: token $FORGEJO_TOKEN" \
    -H "Content-Type: application/json" \
    ${body:+-d "$body"}
}

echo "==> labels (swarm-ready, role:*, needs-human)"
for label in \
  '{"name":"swarm-ready","color":"#00aa00","description":"queue for the swarm"}' \
  '{"name":"role:backend","color":"#1d76db"}' \
  '{"name":"role:frontend","color":"#5319e7"}' \
  '{"name":"role:review","color":"#fbca04"}' \
  '{"name":"role:triage","color":"#d93f0b"}' \
  '{"name":"needs-human","color":"#b60205","description":"DLQ triage"}'; do
  api POST "/repos/$OWNER/$REPO/labels" "$label" >/dev/null || true # exists
done

echo "==> integration branch (auto-merge target; never main)"
api POST "/repos/$OWNER/$REPO/branches" \
  '{"new_branch_name":"integration","old_branch_name":"main"}' >/dev/null || true

echo "==> protect main (no bot pushes, human gate)"
api POST "/repos/$OWNER/$REPO/branch_protections" '{
  "branch_name": "main",
  "enable_push": false,
  "block_on_outdated_branch": true,
  "require_signed_commits": false
}' >/dev/null || true

echo "==> protect integration (merge only via PR with green checks)"
api POST "/repos/$OWNER/$REPO/branch_protections" '{
  "branch_name": "integration",
  "enable_push": false,
  "enable_status_check": true,
  "status_check_contexts": ["gates"],
  "block_on_rejected_reviews": true
}' >/dev/null || true

echo
echo "Done. Remaining manual steps:"
echo "  - create bot user '$BOT' and store its write-scope token in OpenBao:"
echo "      curl -X POST \$OPENBAO_ADDR/v1/secret/data/swarm/forgejo \\"
echo "        -H 'X-Vault-Token: ...' -d '{\"data\":{\"token\":\"<bot token>\"}}'"
echo "  - register a Forgejo Actions runner (label: docker) on a SEPARATE"
echo "    host/node from the Forgejo server, non-privileged"
