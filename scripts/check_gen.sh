#!/usr/bin/env bash
# agent/gen/ is generated from proto/ (`make gen`, grpcio-tools from uv.lock).
# Regenerate and fail on any difference — this is what lets the diff-size
# gate leave agent/gen/** uncounted (Phase-1 review fix).
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --no-sync python -m grpc_tools.protoc -Iproto \
  --python_out=agent/gen --pyi_out=agent/gen \
  proto/swarm/v1/task.proto proto/swarm/v1/event.proto
git diff --exit-code --stat -- agent/gen
echo "agent/gen matches a fresh protoc run"
