#!/usr/bin/env bash
# Sandboxed skill runner (Hard Rule 6): skills NEVER run on the host kernel.
#
# Tier A (this script): podman with no network, read-only rootfs, all caps
#   dropped, seccomp profile, tmpfs workdir only.
# Tier B: the skill-runner pod uses runtimeClassName: gvisor (baseline) or
#   kata (dedicated kernel) — see deploy/k8s/skill-runner.yaml.
#
# Usage: skills/run_skill.sh <skill-name> [args...]
#   Input on stdin, result on stdout. Skills are plain executables in
#   skills/<name>/ with an entrypoint named run.
set -euo pipefail
cd "$(dirname "$0")"

SKILL="${1:?usage: run_skill.sh <skill-name> [args...]}"
shift
[ -d "$SKILL" ] || { echo "unknown skill: $SKILL" >&2; exit 2; }

RUNTIME="$(command -v podman || command -v docker)"
SECCOMP="$(pwd)/skill.seccomp.json"

exec "$RUNTIME" run --rm -i \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  --security-opt "seccomp=$SECCOMP" \
  --tmpfs /work:rw,size=64m \
  --workdir /work \
  -v "$(pwd)/$SKILL:/skill:ro" \
  --memory 512m --cpus 1 --pids-limit 128 \
  python:3.12-slim /skill/run "$@"
