"""Phase-1 review fix — every add-on requirements file installs on top of
requirements.txt: no package is pinned to two different versions."""

import glob
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;]+)")


def pins(path):
    out = {}
    for line in open(path):
        m = PIN.match(line.strip())
        if m:
            out.setdefault(re.sub(r"[-_.]+", "-", m.group(1)).lower(), set()).add(m.group(2))
    return out


def test_addons_agree_with_requirements_txt():
    base = pins(os.path.join(ROOT, "requirements.txt"))
    standalone = ("requirements-ingest.txt", "requirements-elevation.txt")
    addons = [p for p in glob.glob(os.path.join(ROOT, "requirements-*.txt"))
              if not p.endswith(standalone)]  # fmt: skip
    assert addons
    seen = {}  # name -> (file, versions) across all add-ons
    for path in addons:
        name_file = os.path.basename(path)
        for name, versions in pins(path).items():
            assert name not in base, f"{name_file} re-pins {name} from requirements.txt"
            if name in seen:
                assert seen[name][1] == versions, (name, seen[name], (name_file, versions))
            seen[name] = (name_file, versions)
