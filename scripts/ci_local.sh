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

step "generated protobuf code matches proto/"
./scripts/check_gen.sh

step "headroom environment from uv.lock (isolated — never the agent env)"
# The locked `headroom-proxy` group: what deploy/Containerfile.headroom
# installs. The proxy start-up test and the headroom gate both use it.
export HEADROOM_PROXY_ENV="${TMPDIR:-/tmp}/headroom-env"
UV_PROJECT_ENVIRONMENT="$HEADROOM_PROXY_ENV" \
  uv sync -q --frozen --no-install-project --only-group headroom-proxy

step "lint (ruff)"
uv run --no-sync ruff check .

step "tests + coverage floor"
uv run --no-sync pytest --cov=agent --cov=ingest --cov-fail-under=20 -q

step "security scan (bandit)"
uv run --no-sync bandit -q -r agent ingest scripts -x tests --severity-level medium

step "diff-size limit"
bash .forgejo/scripts/diff_size_gate.sh

step "AGENTS.md size gate (cross-tool constitution, v4.2 T7.5)"
uv run --no-sync python .forgejo/scripts/agents_md_gate.py

step "headroom gate (the locked headroom environment)"
"$HEADROOM_PROXY_ENV/bin/python" .forgejo/scripts/headroom_gate.py

step "license gate: Python lock from publisher metadata (zone C allowlist on what ships, zone B denylist)"
# Needs pypi.org or a warm .cache/license-gate/; offline it fails (exit 2).
uv run --no-sync python .forgejo/scripts/license_gate.py --zone C --python-only
uv run --no-sync python .forgejo/scripts/license_gate.py --zone B --python-only

step "license gate: code directories, npm lock and repository (syft)"
if command -v syft >/dev/null 2>&1; then
  uv run --no-sync python .forgejo/scripts/license_gate.py --zone C
  uv run --no-sync python .forgejo/scripts/license_gate.py --zone B
else
  echo "DEFERRED: syft not installed here — the syft part runs in CI and in make verify-integration"
fi

echo
echo "ci-local: all gates passed"
