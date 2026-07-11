#!/usr/bin/env bash
# One-shot Tier A bootstrap: schema -> streams -> dev secrets -> schemas.
# Run after `make up` (or `podman compose -f deploy/compose.yaml up -d`).
set -euo pipefail
cd "$(dirname "$0")/.."

DATABASE_URL="${DATABASE_URL:-postgres://postgres:dev@localhost:5432/swarm}"
OPENBAO_ADDR="${OPENBAO_ADDR:-http://localhost:8200}"
OPENBAO_TOKEN="${OPENBAO_TOKEN:-dev-root}"
APICURIO_URL="${APICURIO_URL:-http://localhost:8081}"
RUNTIME="$(command -v podman || command -v docker)"

echo "==> waiting for postgres"
for _ in $(seq 1 60); do
  if "$RUNTIME" exec "$("$RUNTIME" ps -qf name=postgres | head -1)" \
      pg_isready -U postgres -d swarm >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "==> applying db/schema.sql (idempotent)"
if command -v psql >/dev/null 2>&1; then
  psql "$DATABASE_URL" -f db/schema.sql
else
  "$RUNTIME" exec -i "$("$RUNTIME" ps -qf name=postgres | head -1)" \
    psql -U postgres -d swarm < db/schema.sql
fi

echo "==> creating JetStream streams + consumers"
./scripts/bootstrap_jetstream.sh

echo "==> seeding OpenBao dev secrets (placeholders — replace with real tokens)"
curl -sf -X POST "$OPENBAO_ADDR/v1/secret/data/swarm/forgejo" \
  -H "X-Vault-Token: $OPENBAO_TOKEN" \
  -d '{"data":{"token":"REPLACE_WITH_FORGEJO_BOT_TOKEN"}}' >/dev/null
curl -sf -X POST "$OPENBAO_ADDR/v1/secret/data/swarm/telegram" \
  -H "X-Vault-Token: $OPENBAO_TOKEN" \
  -d '{"data":{"token":"REPLACE_WITH_TELEGRAM_BOT_TOKEN"}}' >/dev/null

echo "==> registering Protobuf schemas with Apicurio (compatibility-gated)"
./scripts/register_schemas.sh || echo "    (apicurio not ready yet — rerun scripts/register_schemas.sh)"

echo
echo "Bootstrap complete. Next steps:"
echo "  1. Pull a model:        $RUNTIME exec -it \$($RUNTIME ps -qf name=ollama | head -1) ollama pull maxwell1500/ornith-9b:Q4_K_M"
echo "  2. Store real secrets:  curl -X POST $OPENBAO_ADDR/v1/secret/data/swarm/forgejo -H 'X-Vault-Token: $OPENBAO_TOKEN' -d '{\"data\":{\"token\":\"<bot token>\"}}'"
echo "  3. Start the swarm:     make swarm   (relay + backend agent)"
echo "  4. Inject a test task:  make inject"
