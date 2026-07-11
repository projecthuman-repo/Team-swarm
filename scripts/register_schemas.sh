#!/usr/bin/env bash
# Register the Protobuf contracts with Apicurio Registry (runbook §3).
# Apicurio's compatibility rules then reject breaking schema changes.
set -euo pipefail
cd "$(dirname "$0")/.."

APICURIO_URL="${APICURIO_URL:-http://localhost:8081}"

register() {
  local artifact_id="$1" proto_file="$2"
  curl -sf -X POST \
    "$APICURIO_URL/apis/registry/v3/groups/swarm/artifacts?ifExists=FIND_OR_CREATE_VERSION" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg id "$artifact_id" --rawfile content "$proto_file" \
      '{artifactId: $id, artifactType: "PROTOBUF",
        firstVersion: {content: {content: $content,
                                 contentType: "application/x-protobuf"}}}')" \
    >/dev/null
  echo "registered swarm/$artifact_id"
}

register Task proto/swarm/v1/task.proto
register AgentEvent proto/swarm/v1/event.proto

# Enforce backward compatibility on the group (breaking changes get rejected)
curl -sf -X POST "$APICURIO_URL/apis/registry/v3/groups/swarm/rules" \
  -H "Content-Type: application/json" \
  -d '{"ruleType":"COMPATIBILITY","config":"BACKWARD"}' >/dev/null 2>&1 \
  || true # already set
echo "compatibility rule: BACKWARD"
