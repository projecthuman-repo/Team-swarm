#!/usr/bin/env bash
# `make clean` (Lean & Precise L1.5): reclaim container disk.
#   1. <runtime> system prune -f  — stopped containers, dangling images,
#      unused networks, build cache
#   2. remove dangling volumes    — EXCEPT this compose project's named
#      volumes (deploy_pg-data, deploy_nats-data, ...): after `make down`
#      they are dangling too, and they hold the task table and the lessons.
#      Dropping swarm data is explicit: `<runtime> compose -f deploy/compose.yaml down -v`.
set -uo pipefail
RUNTIME="$(command -v podman || command -v docker || true)"
PROJECT="${COMPOSE_PROJECT_NAME:-deploy}"
[ -n "$RUNTIME" ] || { echo "clean: neither podman nor docker found" >&2; exit 1; }

"$RUNTIME" system prune -f || exit 1

removed=0 kept=0
for vol in $("$RUNTIME" volume ls -q --filter dangling=true); do
  owner="$("$RUNTIME" volume inspect --format \
    '{{ index .Labels "com.docker.compose.project" }}' "$vol" 2>/dev/null || true)"
  case "$vol" in
    "${PROJECT}_"*) owner="$PROJECT" ;;
  esac
  if [ "$owner" = "$PROJECT" ]; then
    echo "kept    $vol (swarm data — compose project $PROJECT)"
    kept=$((kept + 1))
  elif "$RUNTIME" volume rm "$vol" >/dev/null; then
    echo "removed $vol"
    removed=$((removed + 1))
  fi
done
echo "clean: removed $removed dangling volume(s), kept $kept swarm volume(s)"
