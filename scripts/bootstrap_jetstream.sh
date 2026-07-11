#!/usr/bin/env bash
# Create the JetStream streams and durable pull consumers (runbook §5,
# v4.1 T4.2 tier subjects + T5.1 geometric backoff ladder). Idempotent.
#
#   REPLICAS=1 (Tier A default) | 3 (Tier B HA)
#   NATS_URL=nats://localhost:4222
#   TIERS="9b-q4 35b-q4"   model tiers to pre-create consumers for
#
# Subjects: swarm.tasks.<role> is the v4.0/default tier (backward
# compatible); swarm.tasks.<role>.<tier> routes capacity-labelled tasks
# (T4.2). Consumers use the explicit backoff ladder 1s,5s,1m,5m,10m with
# max-deliver=5 (T5.1) — the 5th failure emits MAX_DELIVERIES -> SWARM_DLQ.
set -euo pipefail

NATS_URL="${NATS_URL:-nats://localhost:4222}"
REPLICAS="${REPLICAS:-1}"
ROLES=(backend frontend review triage)
TIERS=(${TIERS:-9b-q4 35b-q4})

CFG_DIR="$(mktemp -d)"
trap 'rm -rf "$CFG_DIR"' EXIT

if command -v nats >/dev/null 2>&1; then
  NATS=(nats -s "$NATS_URL")
else
  RUNTIME="$(command -v podman || command -v docker)"
  # host network so the container can reach the compose-published port;
  # mount the config dir so --config files are visible inside.
  NATS=("$RUNTIME" run --rm --network=host -v "$CFG_DIR:$CFG_DIR:ro" \
    natsio/nats-box:latest nats -s "$NATS_URL")
fi

stream_exists() { "${NATS[@]}" stream info "$1" >/dev/null 2>&1; }
consumer_exists() { "${NATS[@]}" consumer info "$1" "$2" >/dev/null 2>&1; }

# Geometric backoff ladder in nanoseconds: 1s, 5s, 1m, 5m, 10m (T5.1)
BACKOFF_NS='[1000000000, 5000000000, 60000000000, 300000000000, 600000000000]'

write_consumer_cfg() { # $1=durable $2=filter subject
  cat > "$CFG_DIR/$1.json" <<EOF
{
  "durable_name": "$1",
  "filter_subject": "$2",
  "ack_policy": "explicit",
  "max_deliver": 5,
  "backoff": $BACKOFF_NS,
  "max_ack_pending": 8
}
EOF
}

add_consumer() { # $1=durable $2=filter
  if ! consumer_exists SWARM_TASKS "$1"; then
    write_consumer_cfg "$1" "$2"
    "${NATS[@]}" consumer add SWARM_TASKS "$1" --config "$CFG_DIR/$1.json"
  fi
}

# Work-queue stream for tasks — 'swarm.tasks.>' covers role AND role.tier
if ! stream_exists SWARM_TASKS; then
  "${NATS[@]}" stream add SWARM_TASKS \
    --subjects='swarm.tasks.>' --storage=file --replicas="$REPLICAS" \
    --retention=work --discard=old --max-age=24h --dupe-window=2m \
    --defaults
fi

for role in "${ROLES[@]}"; do
  # default tier: the unchanged v4.0 subject
  add_consumer "$role" "swarm.tasks.$role"
  # capacity tiers (T4.2): claimed only by nodes that can_serve them
  for tier in "${TIERS[@]}"; do
    add_consumer "$role-$tier" "swarm.tasks.$role.$tier"
  done
done

# Event/audit stream
if ! stream_exists SWARM_EVENTS; then
  "${NATS[@]}" stream add SWARM_EVENTS \
    --subjects='swarm.events.>' --storage=file --replicas="$REPLICAS" \
    --retention=limits --max-age=168h --defaults
fi

# DLQ fed by the MAX_DELIVERIES advisory (JetStream has no built-in DLQ)
if ! stream_exists SWARM_DLQ; then
  "${NATS[@]}" stream add SWARM_DLQ \
    --subjects='$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SWARM_TASKS.*' \
    --storage=file --retention=work --discard=new --defaults
fi

echo "JetStream bootstrap complete:"
"${NATS[@]}" stream ls
