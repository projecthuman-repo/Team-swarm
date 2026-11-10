#!/usr/bin/env bash
# Create the `swarm-host` network used by deploy/compose.hostports*.yaml
# (review R5): a non-internal bridge, so published ports are forwarded, with
# outbound NAT disabled in the way each runtime supports. Idempotent.
#   Docker: com.docker.network.bridge.enable_ip_masquerade=false
#   Podman: no_default_route=true (Podman >= 4.6 / netavark >= 1.7)
# Neither is a firewall — see the header of deploy/compose.hostports.yaml.
set -euo pipefail
NET="${SWARM_HOST_NET:-swarm-host}"
RUNTIME="${RUNTIME:-$(command -v podman || command -v docker || true)}"
[ -n "$RUNTIME" ] || { echo "hostports: neither podman nor docker found" >&2; exit 1; }

if "$RUNTIME" network inspect "$NET" >/dev/null 2>&1; then
  echo "hostports: network $NET exists"
  exit 0
fi
case "$(basename "$RUNTIME")" in
  podman)
    "$RUNTIME" network create --opt no_default_route=true "$NET" >/dev/null
    echo "hostports: created $NET (podman, no default route)"
    ;;
  *)
    "$RUNTIME" network create -d bridge \
      -o com.docker.network.bridge.enable_ip_masquerade=false "$NET" >/dev/null
    echo "hostports: created $NET (docker, IP masquerade off)"
    ;;
esac
