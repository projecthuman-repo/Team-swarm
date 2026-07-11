"""The license gate is fail-closed: unknown licenses block, permissive
compounds pass, every denied family blocks."""

import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "license_gate",
    os.path.join(
        os.path.dirname(__file__), "..", ".forgejo", "scripts", "license_gate.py"
    ),
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def sbom(*packages):
    return {
        "packages": [
            {"name": name, "licenseConcluded": lic} for name, lic in packages
        ]
    }


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
