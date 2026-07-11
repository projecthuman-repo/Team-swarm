#!/usr/bin/env bash
# Refresh the vendored Karpathy coding guidelines (runbook Appendix D).
# Run on a host/CI machine WITH egress — agents have none (Hard Rule 1),
# which is exactly why the file is vendored instead of fetched at runtime.
#
# Upstream: multica-ai/andrej-karpathy-skills (MIT -> passes the §19 gate)
set -euo pipefail
cd "$(dirname "$0")/.."

URL="https://raw.githubusercontent.com/multica-ai/andrej-karpathy-skills/main/CLAUDE.md"
TMP="$(mktemp)"
curl -sfL --max-time 60 "$URL" -o "$TMP"

{
  printf '<!-- Vendored from multica-ai/andrej-karpathy-skills (MIT) — Appendix D.\n'
  printf '     Agents have no egress: refresh with scripts/update_karpathy.sh, never at runtime. -->\n\n'
  sed 's/^# CLAUDE.md$/# Agent coding guidelines (Karpathy)/' "$TMP"
} > karpathy-guidelines.md
rm -f "$TMP"

echo "updated karpathy-guidelines.md from upstream"
git diff --stat -- karpathy-guidelines.md
