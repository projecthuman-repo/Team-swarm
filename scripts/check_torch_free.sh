#!/usr/bin/env bash
# L2.3: the default embeddings extra must not pull torch (fastembed runs on
# ONNX Runtime). Counts torch lines in the locked closure of the base deps +
# the `embeddings` extra (uv export, not uv tree — DECISIONS D-014).
set -euo pipefail
cd "$(dirname "$0")/.."
n="$(uv export --frozen --no-default-groups --extra embeddings --no-hashes | grep -ci torch || true)"
echo "torch entries in the embeddings closure: $n"
test "$n" -eq 0
