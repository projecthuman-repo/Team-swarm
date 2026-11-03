"""The license gate is fail-closed: unknown licenses block, permissive
compounds pass, every denied family blocks."""

import importlib.util
import json
import os

spec = importlib.util.spec_from_file_location(
    "license_gate",
    os.path.join(os.path.dirname(__file__), "..", ".forgejo", "scripts", "license_gate.py"),
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def sbom(*packages):
    return {"packages": [{"name": name, "licenseConcluded": lic} for name, lic in packages]}


def test_permissive_pass():
    assert gate.check(sbom(("a", "MIT"), ("b", "Apache-2.0"), ("c", "MPL-2.0"))) == []


def test_compound_permissive_passes():
    assert gate.check(sbom(("a", "(MIT OR Apache-2.0)"))) == []


def test_denied_families_block():
    for lic in ("AGPL-3.0-only", "SSPL-1.0", "BUSL-1.1", "Elastic-2.0"):
        assert gate.check(sbom(("pkg", lic))), lic


def test_fail_closed_on_unknown():
    # Not on the allowlist and not NOASSERTION -> blocked.
    assert gate.check(sbom(("pkg", "Proprietary-EULA")))


def test_compound_with_copyleft_blocks():
    assert gate.check(sbom(("pkg", "(MIT AND AGPL-3.0-only)")))


# --- Lean & Precise L3.1 — three zones -------------------------------------


def test_agpl_fails_zone_c_and_zone_b():
    s = sbom(("pkg", "AGPL-3.0-only"))
    assert gate.check(s, "C") and gate.check(s, "B")


def test_gplv3_fails_zone_c_but_passes_zone_b():
    s = sbom(("pkg", "GPL-3.0-or-later"))
    assert gate.check(s, "C")
    assert gate.check(s, "B") == []


def test_zone_b_denylist_and_boost():
    for lic in ("SSPL-1.0", "BUSL-1.1", "LicenseRef-Commons-Clause", "(MIT AND AGPL-3.0-only)"):
        assert gate.check(sbom(("pkg", lic)), "B"), lic
    assert gate.check(sbom(("boost", "BSL-1.0"), ("x", "LGPL-2.1-only")), "B") == []
    assert gate.check(sbom(("unknown", "Proprietary-EULA")), "B") == []  # B is a denylist


def test_zone_c_default_is_unchanged():
    s = sbom(("a", "MIT"), ("b", "Elastic-2.0"))
    assert gate.check(s) == gate.check(s, "C") == [("b", "Elastic-2.0")]


def test_services_table_lists_copyleft_services_as_zone_a(capsys):
    rows = {r["service"]: r for r in gate.services_table()}
    assert rows["Forgejo"]["license"] == "GPL-3.0-or-later"
    assert rows["OpenObserve"]["license"] == "AGPL-3.0"
    assert rows["Grafana"]["license"] == "AGPL-3.0"
    assert all(r["zone"] == "A" for r in rows.values())
    assert gate.main(["--zone", "A"]) == 0
    out = capsys.readouterr().out
    assert "Forgejo" in out and "OpenObserve" in out and "Grafana" in out


def test_every_compose_image_has_a_zone_a_row():
    import yaml

    with open(os.path.join(gate.ROOT, "deploy", "compose.yaml")) as f:
        services = yaml.safe_load(f)["services"].values()
    images = {s["image"] for s in services if "image" in s}
    listed = {r["image"] for r in gate.services_table()}
    assert images <= listed, images - listed


def test_paths_scan_per_path_and_fail_closed(monkeypatch):
    calls = []

    def fake_syft(cmd, cwd):
        calls.append(cmd[-1])
        lic = "GPL-3.0-only" if cmd[-1].endswith("uv.lock") else "MIT"
        pkg = {"name": "dep", "versionInfo": "1", "licenseConcluded": lic}
        return json.dumps({"packages": [pkg, pkg]}).encode()

    monkeypatch.setattr(gate.subprocess, "check_output", fake_syft)
    assert gate.main(["--zone", "C", "--paths", "agent", "pyproject.toml"]) == 0
    assert calls[0].startswith("dir:") and calls[1].startswith("file:")
    assert gate.main(["--zone", "C", "--paths", "uv.lock"]) == 1
    assert gate.main(["--zone", "B", "--paths", "uv.lock"]) == 0
    assert gate.main(["--zone", "C", "--paths", "no/such/path"]) == 2
    assert gate.scan(["agent", "agent"])["packages"] == [
        {"name": "dep", "versionInfo": "1", "licenseConcluded": "MIT"}]  # fmt: skip
    assert gate.DEFAULT_PATHS["B"] == ["dir:."]
    for path in gate.DEFAULT_PATHS["C"]:
        assert os.path.exists(os.path.join(gate.ROOT, path)), path
