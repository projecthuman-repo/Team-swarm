"""L1.5 — the lite spine stays a strict subset of the full compose file."""

import os
import re
import stat
import subprocess

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LITE = {"nats", "postgres", "valkey", "openbao"}


def load(name):
    with open(os.path.join(ROOT, "deploy", name)) as f:
        return yaml.safe_load(f)


def test_lite_services_are_copies_of_the_full_file():
    full, lite = load("compose.yaml"), load("compose.lite.yaml")
    assert set(lite["services"]) == LITE
    for name, svc in lite["services"].items():
        assert svc == full["services"][name], f"{name} drifted from deploy/compose.yaml"
        assert "profiles" not in svc
    assert lite["networks"] == full["networks"]
    for name, spec in lite["volumes"].items():
        assert full["volumes"][name] == spec
    used = {
        v.split(":")[0]
        for svc in lite["services"].values()
        for v in svc.get("volumes", [])
        if not v.startswith((".", "/"))
    }
    assert used <= set(lite["volumes"])


def makefile_recipe(target):
    text = open(os.path.join(ROOT, "Makefile")).read()
    m = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n?)+)", text, re.M)
    assert m, f"Makefile target {target} missing"
    return m.group(1)


def test_lite_make_targets():
    text = open(os.path.join(ROOT, "Makefile")).read()
    assert re.search(r"^COMPOSE_LITE := .* -f deploy/compose\.lite\.yaml$", text, re.M)
    assert "$(COMPOSE_LITE) up -d" in makefile_recipe("up-lite")
    assert "$(COMPOSE_LITE) down" in makefile_recipe("down-lite")
    assert re.search(r"\$\(COMPOSE\) up -d ollama\s*$", makefile_recipe("ollama"))
    assert "scripts/clean.sh" in makefile_recipe("clean")
    assert "system df" in makefile_recipe("disk-usage")
    # forgejo names its service so the lite path stays lite
    assert makefile_recipe("forgejo").rstrip().endswith("up -d forgejo")
    clean = open(os.path.join(ROOT, "scripts", "clean.sh")).read()
    assert "system prune -f" in clean and "dangling=true" in clean


def run_bootstrap_dry(tmp_path, ps_output):
    fake = tmp_path / "bin"
    fake.mkdir()
    for runtime in ("podman", "docker"):
        exe = fake / runtime
        exe.write_text(f'#!/bin/sh\n[ "$1" = ps ] && printf "{ps_output}"\nexit 0\n')
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{fake}:/usr/bin:/bin")
    return subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "bootstrap.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bootstrap_dry_run_detects_lite_mode(tmp_path):
    res = run_bootstrap_dry(tmp_path, "")
    assert res.returncode == 0, res.stderr
    assert "lite mode: apicurio seaweedfs not running" in res.stdout
    assert "would SKIP Protobuf schema registration" in res.stdout


def test_bootstrap_dry_run_detects_full_spine(tmp_path):
    res = run_bootstrap_dry(tmp_path, "abc123\\n")
    assert res.returncode == 0, res.stderr
    assert "full spine detected" in res.stdout
    assert "would register Protobuf schemas" in res.stdout
