#!/usr/bin/env bash
# `make ci-local` (Lean & Precise L1.2): the gates of
# .forgejo/workflows/ci.yaml, same commands, same order. Keep the two in
# step. The only difference: without syft here, the license gate is
# reported DEFERRED (it runs in CI and in `make verify-integration`).
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PYTHON="${UV_PYTHON:-3.12}"

step() { echo; echo "==> $*"; }

step "locked environment"
uv sync --frozen --group agent --group dev
uv lock --check

step "embeddings extra is torch-free"
./scripts/check_torch_free.sh

step "pre-commit (developer hooks)"
uv run --no-sync pre-commit run --all-files --show-diff-on-failure

step "generated requirements match uv.lock"
sha1sum uv.lock requirements*.txt > "${TMPDIR:-/tmp}/lock.sha"
./scripts/lock_export.sh
sha1sum -c --quiet "${TMPDIR:-/tmp}/lock.sha"

step "lint (ruff)"
uv run --no-sync ruff check .

step "tests + coverage floor"
uv run --no-sync pytest --cov=agent --cov=ingest --cov-fail-under=20 -q

step "security scan (bandit)"
uv run --no-sync bandit -q -r agent ingest scripts -x tests --severity-level medium

step "diff-size limit"
bash .forgejo/scripts/diff_size_gate.sh

step "headroom gate (isolated venv — never the agent env)"
HV="${TMPDIR:-/tmp}/headroom-env"
rm -rf "$HV"
uv venv -q --python 3.12 "$HV"
uv pip install -q --python "$HV/bin/python" headroom-ai==0.31.0
"$HV/bin/python" .forgejo/scripts/headroom_gate.py

step "license gate (fail-closed)"
if command -v syft >/dev/null 2>&1; then
  uv run --no-sync python .forgejo/scripts/license_gate.py
else
  echo "DEFERRED: syft not installed here — the gate runs in CI and in make verify-integration"
fi

echo
echo "ci-local: all gates passed"
