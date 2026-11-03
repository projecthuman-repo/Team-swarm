#!/usr/bin/env bash
# `make torch-cpu-lock` (Lean & Precise L1.1, deferred to a host that can
# reach download.pytorch.org): enable the torch-cpu-source block in
# pyproject.toml, re-lock, and prove the torch extras no longer pull CUDA.
# Idempotent. Commit the resulting pyproject.toml, uv.lock and
# requirements-routing.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PYTHON="${UV_PYTHON:-3.12}"

python3 - <<'EOF'
import re
p = "pyproject.toml"
s = open(p).read()
block = re.search(r"# BEGIN torch-cpu-source.*?# END torch-cpu-source", s, re.S)
assert block, "torch-cpu-source markers missing from pyproject.toml"
body = block.group(0)
enabled = re.sub(r"^# (\[tool\.uv\.sources\]|torch = )", r"\1", body, flags=re.M)
open(p, "w").write(s.replace(body, enabled))
print("torch-cpu-source block enabled")
EOF

./scripts/lock_export.sh

fail=0
for extra in embeddings-torch routing; do
  cuda="$(uv export --frozen --no-hashes --no-header --no-default-groups --extra "$extra" \
          | grep -E '^nvidia-' | grep -v '^nvidia-ml-py' || true)"
  torch="$(uv export --frozen --no-hashes --no-header --no-default-groups --extra "$extra" \
          | grep -E '^torch==' || true)"
  echo "$extra: $torch"
  if [ -n "$cuda" ]; then echo "  FAIL: CUDA wheels still resolved:"; echo "$cuda"; fail=1; fi
  case "$torch" in *+cpu*) ;; *) echo "  FAIL: torch is not the +cpu build"; fail=1;; esac
done
exit "$fail"
