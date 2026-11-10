# ruff: noqa: E501
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

    def fake_syft(cmd, cwd, env):
        calls.append(cmd[-1])
        assert env["SYFT_PYTHON_SEARCH_REMOTE_LICENSES"] == "true"  # uv.lock has no licenses
        lic = "GPL-3.0-only" if cmd[-1].endswith("requirements.txt") else "MIT"
        pkg = {"name": "dep", "versionInfo": "1", **syft_fields(lic)}
        return json.dumps({"packages": [pkg, pkg]}).encode()

    monkeypatch.setattr(gate.subprocess, "check_output", fake_syft)
    assert gate.main(["--zone", "C", "--paths", "agent", "pyproject.toml"]) == 0
    assert calls[0].startswith("dir:") and calls[1].startswith("file:")
    assert gate.main(["--zone", "C", "--paths", "requirements.txt"]) == 1
    assert gate.main(["--zone", "B", "--paths", "requirements.txt"]) == 0
    assert gate.main(["--zone", "C", "--paths", "no/such/path"]) == 2
    assert gate.scan(["agent", "agent"])["packages"] == [
        {"name": "dep", "versionInfo": "1", **syft_fields("MIT")}]  # fmt: skip
    assert gate.DEFAULT_PATHS["B"] == ["dir:."]
    for path in gate.DEFAULT_PATHS["C"]:
        assert os.path.exists(os.path.join(gate.ROOT, path)), path


# --- Phase-3 review: the gate must judge what syft really emits -------------


def syft_fields(declared):
    """syft's shape: metadata licenses are *declared*; concluded stays NOASSERTION."""
    return {"licenseConcluded": "NOASSERTION", "licenseDeclared": declared}


def syft_sbom(*packages):
    return {
        "packages": [{"name": n, "versionInfo": "1", **syft_fields(lic)} for n, lic in packages]
    }


def test_declared_license_is_judged_when_concluded_is_noassertion():
    s = syft_sbom(
        ("a", "AGPL-3.0-only"), ("g", "GPL-3.0-or-later"), ("p", "LicenseRef-Proprietary")
    )
    assert {name for name, _ in gate.check(s, "C")} == {"a", "g", "p"}
    assert gate.check(s, "B") == [("a", "AGPL-3.0-only")]
    assert gate.check(syft_sbom(("ok", "MIT"), ("ok2", "Apache-2.0 AND BSD-2-Clause")), "C") == []


def test_both_fields_are_judged():
    pkg = {"name": "x", "licenseConcluded": "MIT", "licenseDeclared": "AGPL-3.0-only"}
    assert gate.check({"packages": [pkg]}, "C") == [("x", "AGPL-3.0-only")]
    assert gate.check({"packages": [pkg]}, "B") == [("x", "AGPL-3.0-only")]


def test_zone_c_fails_closed_on_a_package_without_any_license():
    s = syft_sbom(("mystery", "NOASSERTION"), ("none", "NONE"))
    assert gate.check(s, "C") == [
        ("mystery", gate.NO_LICENSE_FOUND), ("none", gate.NO_LICENSE_FOUND)]  # fmt: skip
    assert gate.check(s, "B") == []  # B is a denylist
    assert gate.check({"packages": [{"name": "bare"}]}, "C") == [("bare", gate.NO_LICENSE_FOUND)]


def test_overrides_fill_a_missing_license_but_never_replace_a_found_one():
    s = syft_sbom(("Py_Rust.Stemmers", "NOASSERTION"), ("sneaky", "AGPL-3.0-only"))
    over = {"py-rust-stemmers": "MIT", "sneaky": "MIT"}
    assert gate.check(s, "C", over) == [("sneaky", "AGPL-3.0-only")]
    # an override is judged by the same allowlist
    assert gate.check(syft_sbom(("m", "NOASSERTION")), "C", {"m": "GPL-3.0-only"}) == [
        ("m", "GPL-3.0-only")]  # fmt: skip


def test_scan_roots_and_first_party_need_no_license():
    root = {
        "name": "agent",
        "SPDXID": "SPDXRef-DocumentRoot-Directory-agent",
        **syft_fields("NOASSERTION"),
    }
    own = {"name": "overnight-agent-swarm", "versionInfo": "4.1.1", **syft_fields("NOASSERTION")}
    assert gate.check({"packages": [root, own]}, "C", {}, gate.first_party()) == []
    assert gate.check({"packages": [own]}, "C") == [
        ("overnight-agent-swarm", gate.NO_LICENSE_FOUND)
    ]
    assert gate.first_party() == {"overnight-agent-swarm", "swarm-outbox-relay"}


def test_a_license_from_any_scanned_path_counts_for_the_package():
    lock_entry = {"name": "dep", "versionInfo": "1", **syft_fields("NOASSERTION")}
    installed = {"name": "dep", "versionInfo": "1", **syft_fields("MIT")}
    assert gate.check({"packages": [lock_entry, installed]}, "C") == []
    other_version = {"name": "dep", "versionInfo": "2", **syft_fields("NOASSERTION")}
    assert gate.check({"packages": [installed, other_version]}, "C") == [
        ("dep", gate.NO_LICENSE_FOUND)
    ]


def test_override_table_is_reviewed_and_permissive():
    over = gate.license_overrides()
    assert over["protobuf"] == "BSD-3-Clause" and over["sqlite-vec"] == "MIT OR Apache-2.0"
    assert not gate.check(syft_sbom(*[(n, "NOASSERTION") for n in over]), "C", over)
    assert not [n for n in over if "nvidia" in n or "cuda" in n]  # proprietary: never a row


def test_free_text_license_needs_an_override_row_like_a_missing_one():
    # syft: PyPI "3-Clause BSD License" -> LicenseRef-3-Clause-BSD-License (not an SPDX id)
    s = syft_sbom(("protobuf", "LicenseRef-3-Clause-BSD-License"), ("x", "LicenseRef-MIT-License"))
    assert gate.check(s, "C") == [
        ("protobuf", "LicenseRef-3-Clause-BSD-License"), ("x", "LicenseRef-MIT-License")]  # fmt: skip
    assert gate.check(s, "C", {"protobuf": "BSD-3-Clause", "x": "MIT"}) == []
    assert gate.check(s, "C", {"protobuf": "GPL-3.0-only", "x": "MIT"}) == [
        ("protobuf", "GPL-3.0-only")]  # fmt: skip
    assert gate.check(s, "B") == []


def test_override_cannot_launder_a_denied_or_real_license():
    over = {"a": "MIT", "b": "MIT", "c": "MIT"}
    s = syft_sbom(
        ("a", "LicenseRef-GNU-AGPL-v3"),  # free text naming a denied family
        ("b", "GPL-3.0-only"),  # a real SPDX id
        ("c", "MIT AND LicenseRef-Proprietary-EULA"),  # SPDX id plus unmapped text
    )
    assert gate.check(s, "C", over) == [
        ("a", "LicenseRef-GNU-AGPL-v3"),
        ("b", "GPL-3.0-only"),
        ("c", "MIT AND LicenseRef-Proprietary-EULA"),
    ]
    mixed = {"name": "d", "licenseConcluded": "MIT", "licenseDeclared": "LicenseRef-Custom"}
    assert gate.check({"packages": [mixed]}, "C", {"d": "MIT"}) == [("d", "LicenseRef-Custom")]


def test_free_text_naming_a_copyleft_or_proprietary_family_ignores_the_row():
    # review R2: before, only AGPL/SSPL/BUSL/Elastic text was refused
    for text in ("LicenseRef-GNU-GPL-v3", "LicenseRef-LGPL", "LicenseRef-Proprietary",
                 "LicenseRef-Eclipse-Public-License-2.0", "LicenseRef-CDDL-1.0",
                 "LicenseRef-Creative-Commons-Attribution-NonCommercial"):  # fmt: skip
        assert gate.check(syft_sbom(("pkg", text)), "C", {"pkg": "MIT"}) == [("pkg", text)], text
    assert gate.check(syft_sbom(("ok", "LicenseRef-MIT-License")), "C", {"ok": "MIT"}) == []


def test_allowlist_additions_and_boost():
    ok = sbom(("pillow", "MIT-CMU"), ("regex", "Apache-2.0 AND CNRI-Python"), ("cffi", "MIT-0"))
    assert gate.check(ok, "C") == []
    # Boost is not a denied family: Zone B passes it, Zone C leaves it to the allowlist
    boost = sbom(("boost", "BSL-1.0"))
    assert gate.check(boost, "B") == []
    assert not gate.DENIED_C.search("BSL-1.0")
    assert gate.check(boost, "C") == [("boost", "BSL-1.0")]  # not on the allowlist
    for lic in ("BSL", "BUSL-1.1", "LicenseRef-Commons-Clause"):
        assert gate.check(sbom(("x", lic)), "C") and gate.check(sbom(("x", lic)), "B"), lic


ROOT_ENTRY = {
    "name": "x",
    "SPDXID": "SPDXRef-DocumentRoot-Directory-x",
    **syft_fields("NOASSERTION"),
}


def _run_main(monkeypatch, packages, argv):
    """main() with a fake syft; the Python verdicts (PyPI) are stubbed out here
    and tested in tests/test_license_gate_python.py."""
    out = json.dumps({"packages": packages}).encode()
    monkeypatch.setattr(gate.subprocess, "check_output", lambda cmd, cwd, env: out)
    monkeypatch.setattr(gate, "python_verdicts", lambda zone: [])
    return gate.main(argv)


def test_main_fails_when_the_scan_finds_nothing(monkeypatch, capsys):
    # what syft really emits for an empty scan: only its own root entry per source
    for packages in ([], [ROOT_ENTRY]):
        assert _run_main(monkeypatch, packages, ["--zone", "C", "--paths", "agent"]) == 2
        assert _run_main(monkeypatch, packages, ["--zone", "B"]) == 2
        assert "no package at all" in capsys.readouterr().out


def test_main_fails_when_a_lockfile_yields_nothing(monkeypatch, capsys):
    dep = {"name": "dep", "versionInfo": "1", **syft_fields("MIT")}

    def fake_syft(cmd, cwd, env):
        empty = cmd[-1].endswith("package-lock.json")
        return json.dumps({"packages": [ROOT_ENTRY] if empty else [ROOT_ENTRY, dep]}).encode()

    monkeypatch.setattr(gate.subprocess, "check_output", fake_syft)
    assert gate.main(["--zone", "C", "--paths", "agent", "relay/package-lock.json"]) == 2
    assert "nothing catalogued from ['relay/package-lock.json']" in capsys.readouterr().out
    assert gate.main(["--zone", "C", "--paths", "agent", "skills"]) == 0
    assert "zone C: 1 package(s)" in capsys.readouterr().out  # the root entry is not counted


def test_main_zone_c_blocks_syft_shaped_agpl_and_unlicensed(monkeypatch, capsys):
    pk = syft_sbom(("a", "AGPL-3.0-only"))["packages"]
    assert _run_main(monkeypatch, pk, ["--zone", "C", "--paths", "agent"]) == 1
    assert _run_main(monkeypatch, pk, ["--zone", "B"]) == 1
    unknown = syft_sbom(("mystery", "NOASSERTION"))["packages"]
    assert _run_main(monkeypatch, unknown, ["--zone", "C", "--paths", "agent"]) == 1
    assert "no license found" in capsys.readouterr().out
