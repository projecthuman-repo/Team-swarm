"""L3.4 — the docs match the repo: every `make <target>` the README or
RUNBOOK names exists, and every env var the code reads is in .env.example."""

import glob
import json
import os
import re
import tomllib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def makefile_targets():
    return set(re.findall(r"^([a-z][a-z0-9-]*):", read("Makefile"), re.M))


def test_every_documented_make_target_exists():
    targets = makefile_targets()
    for doc in ("README.md", os.path.join("docs", "RUNBOOK.md")):
        named = set(re.findall(r"\bmake ([a-z][a-z0-9-]+)", read(doc)))
        assert named, doc
        assert named <= targets, f"{doc} names missing targets: {sorted(named - targets)}"
    for target in ("lock", "sync", "hooks", "clean", "disk-usage", "footprint",
                   "verify-integration", "up-lite", "ci-local"):  # fmt: skip
        assert f"make {target}" in read("README.md"), target


ENV_PY = re.compile(
    r"os\.environ(?:\.get)?\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
    r"|os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)[\"']\s*\]"
    r"|os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
)
# Set by the platform or the test harness, not by the operator.
NOT_OPERATOR_CONFIG = {"GITHUB_BASE_REF", "PATH", "HOME", "TMPDIR"}


def env_vars_read_by_code():
    found = {}
    patterns = ("agent/*.py", "ingest/*.py", "scripts/*.py", ".forgejo/scripts/*.py",
                "skills/**/*.py")  # fmt: skip
    for pattern in patterns:
        for path in glob.glob(os.path.join(ROOT, pattern), recursive=True):
            for m in ENV_PY.finditer(open(path).read()):
                found.setdefault(next(g for g in m.groups() if g), path)
    for path in glob.glob(os.path.join(ROOT, "relay", "*.ts")):
        for name in re.findall(r"process\.env\.([A-Z][A-Z0-9_]+)", open(path).read()):
            found.setdefault(name, path)
    return found


def test_every_env_var_read_by_code_is_in_env_example():
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", read(".env.example"), re.M))
    used = env_vars_read_by_code()
    assert len(used) > 60  # the scan really sees the codebase
    missing = {k: os.path.relpath(v, ROOT) for k, v in used.items()
               if k not in documented and k not in NOT_OPERATOR_CONFIG}  # fmt: skip
    assert not missing, f"read by code but not in .env.example: {missing}"
    for name in ("EMBEDDING_BACKEND", "QUALITY_FAST_CHECK", "SANDBOX_WORKDIR", "SANDBOX_TMPFS_SIZE",
                 "OUTBOX_RETENTION_DAYS", "VERIFIER_API_BASE", "HEADROOM_BIN"):  # fmt: skip
        assert name in documented, name


def test_versions_and_title():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        assert tomllib.load(f)["project"]["version"] == "4.1.1"
    assert json.loads(read("relay", "package.json"))["version"] == "4.1.1"
    assert json.loads(read("relay", "package-lock.json"))["version"] == "4.1.1"
    assert read("README.md").splitlines()[0].endswith('v4.1.1 "Lean & Precise"')


def test_runbook_documents_migrations_zones_observability_and_footprint():
    runbook = read("docs", "RUNBOOK.md")
    for name in sorted(os.listdir(os.path.join(ROOT, "db", "migrations"))):
        if name.endswith(".sql") and name >= "006":
            assert name in runbook, name
    for needle in ("--zone C", "--zone B", "uv lock --check", "## Footprint (v4.1.1)",
                   "Jaeger", "Perses", "OpenSearch", "Langfuse", "OpenObserve"):  # fmt: skip
        assert needle in runbook, needle
