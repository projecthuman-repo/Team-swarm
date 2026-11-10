#!/usr/bin/env bash
# Review R1, runtime host (`make verify-integration`): the Headroom proxy
# IMAGE starts and answers /health. Uses the image scripts/image_sizes.sh
# builds (swarm-headroom:dev), or builds it. No assets volume is mounted:
# the proxy must come up without one (compression models load lazily).
set -uo pipefail
cd "$(dirname "$0")/.."
RUNTIME="$(command -v podman || command -v docker || true)"
PORT="${HEADROOM_SMOKE_PORT:-18787}"
NAME="swarm-headroom-smoke"
[ -n "$RUNTIME" ] || { echo "headroom-smoke: neither podman nor docker found" >&2; exit 1; }

"$RUNTIME" image inspect swarm-headroom:dev >/dev/null 2>&1 \
  || "$RUNTIME" build -f deploy/Containerfile.headroom -t swarm-headroom:dev . || exit 1
"$RUNTIME" run -d --rm --name "$NAME" -p "127.0.0.1:$PORT:8787" swarm-headroom:dev >/dev/null || exit 1

code=000
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
  [ "$code" = 200 ] && break
  sleep 1
done
if [ "$code" != 200 ]; then
  echo "headroom-smoke: /health never returned 200 (last: $code). Container log:"
  "$RUNTIME" logs "$NAME" 2>&1 | tail -20
fi
"$RUNTIME" stop "$NAME" >/dev/null 2>&1
[ "$code" = 200 ] && echo "headroom image: /health 200"
[ "$code" = 200 ]
