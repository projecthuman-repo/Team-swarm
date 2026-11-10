"""Review R2 — the Python side of the license gate.

Verdicts come from the publisher's PyPI metadata for the locked version
(recorded fixtures in tests/fixtures/license_gate/, copied from pypi.org on
2026-11-10), the allowlist applies to what ships (image closures by name
and version), and no override row can launder a copyleft or proprietary
license.
"""

import importlib.util
import json
import os
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "license_gate")

spec = importlib.util.spec_from_file_location(
    "license_gate_py", os.path.join(ROOT, ".forgejo", "scripts", "license_gate.py")
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

NUMPY, SCIPY = ("numpy", "1.26.4"), ("scipy", "1.15.3")
NVIDIA, NVIDIA_TEXT = ("nvidia-cublas", "13.4.1.3"), ("nvidia-cusparselt-cu13", "0.8.1")
RECORDED = [NUMPY, SCIPY, NVIDIA, NVIDIA_TEXT, ("pillow", "12.1.1"), ("regex", "2026.1.15"),
            ("jinja2", "3.1.6"), ("fsspec", "2026.7.0"), ("cffi", "2.1.1"), ("numpy", "2.5.4"),
            ("protobuf", "7.36.2"), ("python-dateutil", "2.9.0.post0")]  # fmt: skip


def meta(name, version):
    with open(os.path.join(FIXTURES, f"{name}-{version}.json")) as f:
        return json.load(f)


def metas(*keys):
    return {key: meta(*key) for key in keys}


def text_meta(text="", classifiers=(), expression=""):
    return {"license_expression": expression, "license": text, "classifiers": list(classifiers)}


def judge(metadata, shipped, zone="C", overrides=None, acks=None):
    return gate.judge_python(metadata, {"agent": set(shipped)}, zone, overrides, acks)[0]


ACKS = gate.acknowledgements()


def text_of(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


# --- where the declaration comes from ---------------------------------------


def test_declaration_order_expression_then_spdx_field_then_classifiers():
    assert gate.declared(meta("pillow", "12.1.1")) == ("MIT-CMU", "expression")
    assert gate.declared(text_meta("mit")) == ("MIT", "license field")
    assert gate.declared(text_meta("Apache-2.0 AND CNRI-Python")) == (
        "Apache-2.0 AND CNRI-Python", "license field")  # fmt: skip
    assert gate.declared(meta("jinja2", "3.1.6")) == ("BSD-2-Clause OR BSD-3-Clause", "classifiers")
    assert gate.declared(meta("python-dateutil", "2.9.0.post0")) == (
        "Apache-2.0 AND (BSD-2-Clause OR BSD-3-Clause)", "classifiers")  # fmt: skip
    # free text is not a declaration; neither is a classifier outside the map
    assert gate.declared(meta("protobuf", "7.36.2")) == (None, "")
    assert gate.declared(meta("fsspec", "2026.7.0")) == (None, "")
    odd = ["License :: OSI Approved :: MIT License", "License :: Other/Proprietary License"]
    assert gate.declared(text_meta(classifiers=odd)) == (None, "")
    # a long `license` field is a license text, never parsed as an expression
    assert gate.declared(text_meta("MIT " * 60)) == (None, "")
    assert gate.declared(meta(*NUMPY)) == ("BSD-2-Clause OR BSD-3-Clause", "classifiers")


def test_allowlist_additions_pass_for_what_ships():
    keys = [("pillow", "12.1.1"), ("regex", "2026.1.15"), ("cffi", "2.1.1"), ("numpy", "2.5.4")]
    assert judge(metas(*keys), keys) == []
    for lic in ("MIT-CMU", "CNRI-Python", "MIT-0", "Zlib"):
        assert lic in gate.ALLOWED


def test_boost_is_not_a_denied_family_but_business_source_is():
    boost = {
        ("b", "1"): text_meta(expression="BSL-1.0"),
        ("s", "1"): text_meta(expression="BUSL-1.1"),
    }
    assert judge(boost, [], zone="B") == [("s 1", "BUSL-1.1")]
    bad = judge(boost, list(boost))
    assert [name for name, _ in bad] == ["b 1", "s 1"]  # C: neither is on the allowlist
    assert not gate.DENIED_C.search("BSL-1.0") and gate.DENIED_C.search("BSL")


# --- no laundering -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "GNU General Public License v3",
        "GPLv2+",
        "LGPL",
        "GNU Lesser General Public License",
        "Eclipse Public License 2.0",
        "EPL-2.0 text follows …",
        "Proprietary — all rights reserved",
        "Free for non-commercial use",
        "Creative Commons Attribution 4.0",
        "CDDL",
    ],
)
def test_an_override_row_cannot_clear_copyleft_or_proprietary_text(text):
    key = ("sneaky", "1.0")
    bad = judge({key: text_meta(text)}, [key], overrides={"sneaky": "MIT"})
    assert len(bad) == 1 and "override row cannot clear" in bad[0][1], bad


def test_classifiers_naming_a_refused_family_block_too():
    key = ("sneaky", "1.0")
    gpl = ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]
    assert judge({key: text_meta(classifiers=gpl)}, [key], overrides={"sneaky": "MIT"})
    # an honest MIT text with a row still passes
    assert (
        judge({key: text_meta("The MIT License (MIT) …")}, [key], overrides={"sneaky": "MIT"}) == []
    )


def test_numpy_and_scipy_block_without_their_acknowledgement():
    for key in (NUMPY, SCIPY):
        bad = judge(metas(key), [key], overrides={key[0]: "BSD-3-Clause"})
        assert len(bad) == 1 and "GPL" in bad[0][1] and "override row cannot clear" in bad[0][1]
        # an acknowledgement for another version does not count
        other = {(key[0], "9.9.9"): ACKS[key]}
        assert judge(metas(key), [key], overrides={key[0]: "BSD-3-Clause"}, acks=other) == bad
        # with the rows from conventions/licensing.md it passes, on its BSD classifier
        assert judge(metas(key), [key], acks=ACKS) == []


def test_acknowledgement_rows_are_pinned_named_and_limited(tmp_path):
    assert set(ACKS) == {NUMPY, SCIPY}
    for rows in ACKS.values():
        assert {r["license"] for r in rows} == set(gate.ACKNOWLEDGEABLE)
        assert all("lib" in r["bundled library"] and "publisher:" in r["evidence"] for r in rows)
    header = (
        "## Bundled-runtime acknowledgements\n\n"
        "| package | version | bundled library | license | evidence |\n|---|---|---|---|---|\n"
    )
    for row in ("| x | 1 | libfoo | GPL-3.0-only | e |", "| x | 1 |  | LGPL-2.1-or-later | e |"):
        bad = tmp_path / "licensing.md"
        bad.write_text(header + row + "\n")
        with pytest.raises(gate.GateError):
            gate.acknowledgements(str(bad))


def test_acknowledged_packages_are_printed(capsys):
    _, tally = gate.judge_python(metas(NUMPY), {"agent": {NUMPY}}, "C", {}, ACKS)
    gate.report_python(tally, "C", 1)
    out = capsys.readouterr().out
    assert "acknowledged bundled runtime: numpy 1.26.4" in out and "libquadmath" in out
    assert "families in the publisher's text: LGPL, GPL" in out  # R9


# --- review R9: an acknowledgement clears only what it declares --------------

BOTH_ROWS = [{"license": lic, "bundled library": "libx"} for lic in gate.ACKNOWLEDGEABLE]
LGPL_ROW = [{"license": "LGPL-2.1-or-later", "bundled library": "libquadmath"}]


def _ack(key, rows=None):
    return {key: BOTH_ROWS if rows is None else rows}


@pytest.mark.parametrize(
    "text,classifiers,family",
    [
        ("Bundles libfoo (LGPL-2.1-or-later). The rest is Proprietary.", (), "proprietary"),
        ("libquadmath LGPL-2.1-or-later; plugin under the Eclipse Public License 2.0", (), "EPL"),
        ("LGPL runtime inside", ["License :: OSI Approved :: GNU Affero General Public License v3"],
         "AGPL"),
        ("LGPL runtime; the server is under the GNU Affero General Public License", (), "AGPL"),
        ("LGPL runtime; free for non-commercial use only", (), "non-commercial"),
        ("LGPL runtime; docs under Creative Commons Attribution", (), "Creative Commons"),
    ],
)  # fmt: skip
def test_an_acknowledgement_does_not_clear_other_families(text, classifiers, family):
    key = ("mixed", "1.0")
    bad = judge({key: text_meta(text, classifiers)}, [key], acks=_ack(key))
    assert len(bad) == 1, bad
    assert "does not cover" in bad[0][1] and family in bad[0][1].split("which")[0]


def test_an_acknowledgement_clears_only_the_family_of_its_own_license():
    key = NUMPY  # the real text names GPL (libgfortran) and LGPL (libquadmath)
    bad = judge(metas(key), [key], acks=_ack(key, LGPL_ROW))
    assert len(bad) == 1 and "names GPL," in bad[0][1] and "(LGPL) does not cover" in bad[0][1]
    assert judge(metas(key), [key], acks=_ack(key)) == []


def test_real_numpy_and_scipy_name_only_gpl_and_lgpl_once_the_boilerplate_is_gone():
    for key in (NUMPY, SCIPY):
        text = meta(*key)["license"]
        # the standard GPL-3.0 text they reproduce says "Affero", "proprietary", "noncommercially"
        raw = " ".join(text.split())
        assert "GNU Affero General Public License" in raw and "noncommercially" in raw
        assert sorted(gate.refused_families(text)) == ["GPL", "LGPL"]
        _, tally = gate.judge_python(metas(key), {"agent": {key}}, "C", {}, ACKS)
        assert tally["acknowledged"][0][2] == ["LGPL", "GPL"]


def test_only_the_verbatim_standard_sentences_are_removed():
    section_13 = (
        "13. Use with the GNU Affero General Public License.\n  Notwithstanding any other "
        "provision of this License, you have permission … will apply to the combination as such."
    )
    assert gate.refused_families(section_13) == []
    # the same words outside the standard sentences still count
    assert gate.refused_families(section_13 + " Also AGPL-3.0 for the server.") == ["AGPL"]
    assert gate.refused_families("This program is proprietary.") == ["proprietary"]
    assert gate.refused_families("could make it effectively proprietary") == ["proprietary"]
    assert gate.refused_families("allowed only occasionally and noncommercially") == [
        "non-commercial"]  # fmt: skip


# --- scope: what ships -------------------------------------------------------


def test_nvidia_wheel_outside_every_closure_passes_c_and_b_but_blocks_inside_one():
    m = metas(NVIDIA, NVIDIA_TEXT)
    assert judge(m, []) == [] and judge(m, [], zone="B") == []
    bad = dict(judge(m, list(m)))
    assert bad["nvidia-cublas 13.4.1.3"] == gate.NO_LICENSE_FOUND
    assert "proprietary" in bad["nvidia-cusparselt-cu13 0.8.1"]
    # …and a row cannot rescue the proprietary one
    assert judge(metas(NVIDIA_TEXT), [NVIDIA_TEXT], overrides={"nvidia-cusparselt-cu13": "MIT"})


def test_closures_match_by_name_and_version():
    m = metas(NUMPY, ("numpy", "2.5.4"))
    m[("numpy", "2.5.4")] = text_meta(expression="GPL-3.0-only")  # pretend the other fork is GPL
    assert judge(m, [NUMPY], acks=ACKS) == []  # 2.5.4 is in no image: Zone B denylist only
    assert judge(m, [NUMPY, ("numpy", "2.5.4")], acks=ACKS) == [
        ("numpy 2.5.4", "GPL-3.0-only (from the publisher's expression)")]  # fmt: skip


def test_nothing_declared_needs_a_row_and_the_row_is_judged():
    key = ("fsspec", "2026.7.0")
    assert judge(metas(key), [key]) == [("fsspec 2026.7.0", gate.NO_LICENSE_FOUND)]
    assert judge(metas(key), [key], overrides={"fsspec": "BSD-3-Clause"}) == []
    bad = judge(metas(key), [key], overrides={"fsspec": "GPL-3.0-only"})
    assert bad == [("fsspec 2026.7.0", "GPL-3.0-only (from the publisher's override row)")]


def test_entries_in_no_image_are_judged_by_the_denylist():
    agpl = ["License :: OSI Approved :: GNU Affero General Public License v3"]
    m = {
        ("a", "1"): text_meta(expression="AGPL-3.0-only"),
        ("b", "1"): text_meta(classifiers=agpl),
        ("c", "1"): text_meta(expression="GPL-3.0-only"),  # B is a denylist: GPL passes
        ("d", "1"): text_meta(),
    }
    assert [name for name, _ in judge(m, [])] == ["a 1", "b 1"]
    assert [name for name, _ in judge(m, [], zone="B")] == ["a 1", "b 1"]


def test_closures_come_from_uv_export_and_a_failure_is_fatal():
    seen = []

    def export(cmd, cwd, stderr):
        seen.append(cmd)
        return b"# comment\nnumpy==1.26.4 ; python_version < '3.13'\nPy_YAML[extra]==6.0\n"

    out = gate.closures(run=export)
    assert set(out) == {"agent", "ingest", "headroom", "codex"}  # codex: v4.2 lane image (R7)
    assert out["agent"] == {("numpy", "1.26.4"), ("py-yaml", "6.0")}
    base = ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project"]
    assert seen[0] == [*base, "--no-dev", "--group", "agent", "--group", "gate"]
    assert seen[1] == [*base, "--only-group", "ingest"]
    assert seen[2] == [*base, "--only-group", "headroom-proxy"]
    assert seen[3] == seen[0]  # the Codex lane image installs the agent selection

    def failing(cmd, cwd, stderr):
        raise subprocess.CalledProcessError(2, cmd)

    with pytest.raises(gate.GateError):
        gate.closures(run=failing)
    with pytest.raises(gate.GateError):
        gate.closures(run=lambda cmd, cwd, stderr: b"")  # an export that lists nothing


# --- metadata fetch: cached, and offline is exit 2 --------------------------


def test_metadata_is_fetched_once_and_cached(tmp_path, monkeypatch):
    calls = []
    info = {"license_expression": None, "license": "MIT", "classifiers": [
        "Programming Language :: Python", "License :: OSI Approved :: MIT License"]}  # fmt: skip

    def download(url):
        calls.append(url)
        return {"info": info}

    monkeypatch.setattr(gate, "_download", download)
    first = gate.fetch_metadata("Some_Pkg", "1.0", str(tmp_path))
    assert first == {"license_expression": "", "license": "MIT",
                     "classifiers": ["License :: OSI Approved :: MIT License"]}  # fmt: skip
    assert gate.fetch_metadata("some-pkg", "1.0", str(tmp_path)) == first
    assert calls == ["https://pypi.org/pypi/Some_Pkg/1.0/json"]


def _stub_world(monkeypatch, tmp_path, download):
    monkeypatch.setattr(gate, "_download", download)
    monkeypatch.setattr(gate, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(gate, "locked_packages", lambda: [NUMPY, ("fsspec", "2026.7.0")])
    monkeypatch.setattr(
        gate, "closures", lambda: {"agent": {NUMPY, ("fsspec", "2026.7.0")}, "ingest": set()}
    )
    monkeypatch.setattr(
        gate, "load_metadata",
        lambda packages, cache_dir=None: {
            key: gate.fetch_metadata(*key, str(tmp_path / "cache")) for key in packages},
    )  # fmt: skip


def test_python_only_run_offline_is_exit_2(monkeypatch, tmp_path, capsys):
    def offline(url):
        raise OSError("Network is unreachable")

    _stub_world(monkeypatch, tmp_path, offline)
    assert gate.main(["--zone", "C", "--python-only"]) == 2
    assert gate.main(["--zone", "B", "--python-only"]) == 2
    assert "cannot read pypi.org metadata" in capsys.readouterr().out


def test_python_only_run_passes_and_fails_on_recorded_metadata(monkeypatch, tmp_path, capsys):
    def recorded(url):
        name, version = url.split("/")[-3:-1]
        return {"info": meta(name, version)}

    _stub_world(monkeypatch, tmp_path, recorded)
    assert gate.main(["--zone", "C", "--python-only"]) == 0
    out = capsys.readouterr().out
    assert "agent image: 2 pins" in out and "1 by override row" in out
    assert "acknowledged bundled runtime: numpy 1.26.4" in out and "syft not run" in out

    # a failed uv export is exit 2, not a pass
    def no_export():
        raise gate.GateError("`uv export` failed")

    monkeypatch.setattr(gate, "closures", no_export)
    assert gate.main(["--zone", "C", "--python-only"]) == 2
    # --paths selects syft paths; it cannot silently skip the lock
    with pytest.raises(SystemExit):
        gate.main(["--zone", "C", "--python-only", "--paths", "agent"])


def test_full_run_merges_python_and_syft_verdicts(monkeypatch):
    dep = {"name": "left-pad", "versionInfo": "1", "licenseConcluded": "NOASSERTION",
           "licenseDeclared": "MIT"}  # fmt: skip
    out = json.dumps({"packages": [dep]}).encode()
    monkeypatch.setattr(gate.subprocess, "check_output", lambda cmd, cwd, env: out)
    monkeypatch.setattr(gate, "python_verdicts", lambda zone: [])
    assert gate.main(["--zone", "C"]) == 0
    monkeypatch.setattr(gate, "python_verdicts", lambda zone: [("numpy 1.26.4", "GPL")])
    assert gate.main(["--zone", "C"]) == 1


# --- the repository's own configuration -------------------------------------


def test_zone_c_syft_paths_and_image_selections_match_the_containerfiles():
    assert gate.DEFAULT_PATHS["C"] == [
        "agent", "ingest", "relay", "skills", "scripts", "relay/package-lock.json",
        "deploy/codex/package-lock.json"]  # fmt: skip
    for image, selection in gate.IMAGES.items():
        text = open(os.path.join(ROOT, "deploy", f"Containerfile.{image}")).read()
        sync = next(line for line in text.splitlines() if "uv sync" in line)
        for flag in " ".join(selection).replace("--no-dev ", "").split(" --"):
            assert flag.removeprefix("--") in sync.replace("--", ""), (image, flag)


def test_every_recorded_fixture_is_real_metadata_shape():
    for name, version in RECORDED:
        assert set(meta(name, version)) == {"license_expression", "license", "classifiers"}


def test_ci_local_runs_the_python_verdict():
    src = open(os.path.join(ROOT, "scripts", "ci_local.sh")).read()
    assert "license_gate.py --zone C --python-only" in src
    assert ".cache/" in open(os.path.join(ROOT, ".gitignore")).read()


# --- review of R2: what the gate cannot judge is exit 2, never a pass ----------

LOCK_HEAD = """version = 1
[[package]]
name = "team-swarm"
version = "4.2.0"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.32.5"
source = { registry = "https://pypi.org/simple" }
"""


def _lock(tmp_path, extra=""):
    (tmp_path / "uv.lock").write_text(LOCK_HEAD + extra)
    return str(tmp_path)


def test_a_torch_cpu_lock_entry_is_judged_by_its_public_version(tmp_path, monkeypatch):
    # what a successful `make torch-cpu-lock` writes (scripts/torch_cpu_lock.sh)
    root = _lock(
        tmp_path,
        """
[[package]]
name = "torch"
version = "2.15.0+cpu"
source = { registry = "https://download.pytorch.org/whl/cpu" }
""",
    )
    assert gate.locked_packages(root) == [("requests", "2.32.5"), ("torch", "2.15.0+cpu")]
    assert "https://download.pytorch.org/whl/cpu" in text_of("pyproject.toml")  # the declared index
    calls = []

    def download(url):
        calls.append(url)
        return {"info": {"license": "BSD-3-Clause", "classifiers": []}}

    monkeypatch.setattr(gate, "_download", download)
    cache = tmp_path / "cache"
    found = gate.fetch_metadata("torch", "2.15.0+cpu", str(cache))
    assert calls == ["https://pypi.org/pypi/torch/2.15.0/json"]  # pypi.org has no +cpu version
    assert found["license"] == "BSD-3-Clause"
    assert (cache / "torch-2.15.0+cpu.json").exists()  # cached under what the lock says
    assert judge({("torch", "2.15.0+cpu"): found}, [("torch", "2.15.0+cpu")]) == []


def test_the_python_run_passes_on_a_cpu_lock_and_names_the_entry(monkeypatch, tmp_path, capsys):
    torch = ("torch", "2.15.0+cpu")
    monkeypatch.setattr(gate, "locked_packages", lambda: [("fsspec", "2026.7.0"), torch])
    monkeypatch.setattr(gate, "closures", lambda: {"agent": {("fsspec", "2026.7.0")}})
    monkeypatch.setattr(
        gate, "load_metadata",
        lambda packages, cache_dir=None: {
            ("fsspec", "2026.7.0"): meta("fsspec", "2026.7.0"),
            torch: text_meta("BSD-3-Clause"),
        },
    )  # fmt: skip
    assert gate.main(["--zone", "C", "--python-only"]) == 0
    assert gate.main(["--zone", "B", "--python-only"]) == 0
    out = capsys.readouterr().out
    assert (
        "alternate index: torch 2.15.0+cpu judged by the pypi.org metadata of torch 2.15.0" in out
    )


@pytest.mark.parametrize(
    "source",
    [
        '{ registry = "https://packages.example.invalid/simple" }',  # an index nobody reviewed
        '{ git = "https://example.invalid/evil.git?rev=abc123#abc123" }',
        '{ url = "https://example.invalid/evil-1.0-py3-none-any.whl" }',
        '{ path = "vendor/evil-1.0-py3-none-any.whl" }',
        '{ directory = "vendor/evil" }',
        '{ editable = "vendor/evil" }',
    ],
)
def test_a_lock_entry_the_gate_cannot_judge_is_exit_2(tmp_path, monkeypatch, capsys, source):
    root = _lock(tmp_path, f'\n[[package]]\nname = "evil"\nversion = "1.0"\nsource = {source}\n')
    with pytest.raises(gate.GateError, match="evil 1.0 comes from"):
        gate.locked_packages(root)
    monkeypatch.setattr(gate, "ROOT", root)
    real = gate.locked_packages
    monkeypatch.setattr(gate, "locked_packages", lambda: real(root))
    assert gate.main(["--zone", "C", "--python-only"]) == 2
    assert gate.main(["--zone", "B", "--python-only"]) == 2
    assert "LICENSE GATE FAILED (fail-closed)" in capsys.readouterr().out


def test_the_real_lock_has_only_judgeable_sources():
    packages = gate.locked_packages()
    assert len(packages) > 300 and ("team-swarm", "4.2.0") not in packages


@pytest.mark.parametrize(
    "line",
    [
        "evil @ https://example.invalid/evil-1.0-py3-none-any.whl",
        "evil @ git+https://example.invalid/evil.git@abc123",
        "-e ./vendor/evil",
        "./vendor/evil",
    ],
)
def test_an_image_closure_with_something_that_is_not_a_pin_is_exit_2(line):
    header = "# This file was autogenerated by uv\n"
    export = f"{header}requests==2.32.5 ; python_version >= '3.9'\n    # via x\n{line}\n"
    with pytest.raises(gate.GateError, match="installs something that is not a pin"):
        gate.closures(run=lambda cmd, cwd, stderr: export.encode())


def test_a_shipped_pin_without_metadata_is_a_violation():
    bad = judge({}, [("evil", "1.0")])
    assert bad == [("evil 1.0", "in the agent image but not judged: no lock entry")]


@pytest.mark.parametrize(
    "text, family",
    [
        ("Business Source License 1.1", "BUSL"),
        ("BUSL 1.1, see LICENSE", "BUSL"),
        ("BSL", "BUSL"),
        ("Commons Clause + Apache 2.0", "Commons Clause"),
        ("Elastic License 2.0", "Elastic"),
        ("Elastic-2.0 license", "Elastic"),
    ],
)
def test_an_override_row_cannot_clear_the_families_hard_rule_9_names(text, family):
    key = ("pkg", "1.0")
    (violation,) = judge({key: text_meta(text)}, [key], overrides={"pkg": "MIT"})
    assert family in violation[1] and "override row cannot clear" in violation[1]
    assert gate.refused_families("Boost Software License 1.0 (BSL-1.0)") == []
    # the syft path refuses the same free text, LicenseRef- prefix and all
    assert family in gate.refused_families("LicenseRef-" + text.replace(" ", "-"))
