#!/usr/bin/env bash
# Build the swarm images and check their size targets (Lean & Precise L1.4):
#   agent <= 700 MB · ingest <= 300 MB · relay <= 160 MB · headroom <= 760 MB
#   (1 MB = 10^6 bytes of `image inspect .Size`). Builds all four, reports
#   all, then fails if any build failed or any image is over its target.
#   headroom: 565 MB locked environment + ~120 MB python:3.12-slim, +10 %
#   (DECISIONS D-035).
set -uo pipefail
cd "$(dirname "$0")/.."
RUNTIME="$(command -v podman || command -v docker)"
fail=0

check() { # $1 = image (Containerfile suffix), $2 = target MB
  local tag="swarm-$1:dev" bytes mb
  if ! "$RUNTIME" build -f "deploy/Containerfile.$1" -t "$tag" .; then
    echo "BUILD FAILED  $1"
    fail=1
    return
  fi
  bytes="$("$RUNTIME" image inspect --format '{{.Size}}' "$tag")"
  mb=$((bytes / 1000000))
  if [ "$mb" -le "$2" ]; then
    echo "ok    $1: ${mb} MB (target <= $2 MB)"
  else
    echo "OVER  $1: ${mb} MB (target <= $2 MB)"
    fail=1
  fi
}

check agent 700
check ingest 300
check relay 160
check headroom 760
exit "$fail"
