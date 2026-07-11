#!/usr/bin/env bash
# Create the three JetStream streams and per-role durable pull consumers
# (runbook §5). Idempotent: existing streams/consumers are left alone.
#
#   REPLICAS=1 (Tier A default) | 3 (Tier B HA)
#   NATS_URL=nats://localhost:4222
#
# Requires the `nats` CLI (https://github.com/nats-io/natscli). If it is
# not installed, this script runs it via a container instead.
set -euo pipefail

NATS_URL="${NATS_URL:-nats://localhost:4222}"
REPLICAS="${REPLICAS:-1}"
ROLES=(backend frontend review triage)

if command -v nats >/dev/null 2>&1; then
  NATS=(nats -s "$NATS_URL")
else
  RUNTIME="$(command -v podman || command -v docker)"
  # host network so the container can reach the compose-published port
  NATS=("$RUNTIME" run --rm --network=host natsio/nats-box:latest nats -s "$NATS_URL")
fi

stream_exists() { "${NATS[@]}" stream info "$1" >/dev/null 2>&1; }
consumer_exists() { "${NATS[@]}" consumer info "$1" "$2" >/dev/null 2>&1; }

# Work-queue stream for tasks
if ! stream_exists SWARM_TASKS; then
  "${NATS[@]}" stream add SWARM_TASKS \
    --subjects='swarm.tasks.*' --storage=file --replicas="$REPLICAS" \
    --retention=work --discard=old --max-age=24h --dupe-window=2m \
    --max-bytes=-1 --max-msgs=-1 --max-msgs-per-subject=-1 \
    --max-msg-size=-1 --max-consumers=-1 --no-allow-rollup --no-deny-delete \
    --no-deny-purge --defaults
fi

# One durable pull consumer per role
for role in "${ROLES[@]}"; do
  if ! consumer_exists SWARM_TASKS "$role"; then
    "${NATS[@]}" consumer add SWARM_TASKS "$role" \
      --filter="swarm.tasks.$role" --pull --ack=explicit \
      --max-deliver=5 --backoff=linear --backoff-steps=5 \
      --backoff-min=30s --backoff-max=10m \
      --ack-wait=10m --max-pending=8 --defaults
  fi
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
