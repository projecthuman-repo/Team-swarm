#!/usr/bin/env bash
# One-shot Tier A bootstrap: schema -> streams -> dev secrets -> schemas.
# Run after `make up` (or `podman compose -f deploy/compose.yaml up -d`), or
# after `make up-lite`: lite mode (apicurio/seaweedfs not running) skips the
# Protobuf schema registration with a message instead of a failed curl.
#
#   scripts/bootstrap.sh            bootstrap the running spine
#   scripts/bootstrap.sh --dry-run  detect the mode, print every step, touch nothing
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

DATABASE_URL="${DATABASE_URL:-postgres://postgres:dev@localhost:5432/swarm}"
OPENBAO_ADDR="${OPENBAO_ADDR:-http://localhost:8200}"
OPENBAO_TOKEN="${OPENBAO_TOKEN:-dev-root}"
APICURIO_URL="${APICURIO_URL:-http://localhost:8081}"
RUNTIME="$(command -v podman || command -v docker || true)"

running() { # $1 = compose service name; true when a container for it runs
  [ -n "$RUNTIME" ] && [ -n "$("$RUNTIME" ps -qf "name=$1" 2>/dev/null)" ]
}

LITE=0
missing=""
for svc in apicurio seaweedfs; do
  running "$svc" || missing="$missing $svc"
done
if [ -n "$missing" ]; then
  LITE=1
  echo "==> lite mode:${missing} not running (make up-lite) — the steps that need them are skipped"
else
  echo "==> full spine detected"
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "==> dry run: would wait for postgres, ensure the langfuse database,"
  echo "    apply db/schema.sql, create JetStream streams + consumers, seed"
  echo "    OpenBao dev secrets (forgejo telegram slack discord)"
  if [ "$LITE" = 1 ]; then
    echo "==> dry run: would SKIP Protobuf schema registration (apicurio absent)"
  else
    echo "==> dry run: would register Protobuf schemas with Apicurio ($APICURIO_URL)"
  fi
  exit 0
fi
[ -n "$RUNTIME" ] || { echo "bootstrap: neither podman nor docker found" >&2; exit 1; }

echo "==> waiting for postgres"
for _ in $(seq 1 60); do
  if "$RUNTIME" exec "$("$RUNTIME" ps -qf name=postgres | head -1)" \
      pg_isready -U postgres -d swarm >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "==> ensuring langfuse database exists (observability profile)"
"$RUNTIME" exec "$("$RUNTIME" ps -qf name=postgres | head -1)" \
  psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='langfuse'" \
  | grep -q 1 || "$RUNTIME" exec "$("$RUNTIME" ps -qf name=postgres | head -1)" \
  psql -U postgres -c "CREATE DATABASE langfuse"

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
for chat in telegram slack discord; do
  curl -sf -X POST "$OPENBAO_ADDR/v1/secret/data/swarm/$chat" \
    -H "X-Vault-Token: $OPENBAO_TOKEN" \
    -d "{\"data\":{\"token\":\"REPLACE_WITH_${chat^^}_BOT_TOKEN\"}}" >/dev/null
done

if [ "$LITE" = 1 ]; then
  echo "==> skipping Protobuf schema registration: apicurio is not running (lite spine)."
  echo "    The swarm runs without it; the compatibility gate stays in CI (proto-compat)."
  echo "    For the full spine: make up && ./scripts/register_schemas.sh"
else
  echo "==> registering Protobuf schemas with Apicurio (compatibility-gated)"
  ./scripts/register_schemas.sh || echo "    (apicurio not ready yet — rerun scripts/register_schemas.sh)"
fi

echo
echo "Bootstrap complete. Next steps:"
echo "  1. Pull a model:        $RUNTIME exec -it \$($RUNTIME ps -qf name=ollama | head -1) ollama pull maxwell1500/ornith-9b:Q4_K_M"
echo "  2. Store real secrets:  curl -X POST $OPENBAO_ADDR/v1/secret/data/swarm/forgejo -H 'X-Vault-Token: $OPENBAO_TOKEN' -d '{\"data\":{\"token\":\"<bot token>\"}}'"
echo "  3. Start the swarm:     make swarm   (relay + backend agent)"
echo "  4. Inject a test task:  make inject"
