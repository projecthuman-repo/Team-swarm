"""L1.4 — the Containerfiles stay buildable without a container runtime.

Static checks that run in every CI job: each instruction is a real
Dockerfile keyword, every build-context COPY source exists and is not
excluded by .dockerignore, every `COPY --from` names an earlier stage or an
image, and the relay image's CMD points at an entrypoint esbuild actually
emits. The real builds and size targets run in `make verify-integration`.
"""

import fnmatch
import glob
import json
import os
import re
import shlex

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KEYWORDS = {
    "FROM", "RUN", "CMD", "LABEL", "EXPOSE", "ENV", "ADD", "COPY", "ENTRYPOINT",
    "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL",
}  # fmt: skip
CONTAINERFILES = sorted(glob.glob(os.path.join(ROOT, "deploy", "Containerfile.*")))


def instructions(path):
    """Logical instructions (continuations joined, comments dropped)."""
    out, buf = [], ""
    for raw in open(path):
        line = raw.rstrip("\n")
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if buf and line.lstrip().startswith("#"):
            continue  # comment inside a continued RUN
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    return out


def dockerignore_patterns():
    pats = []
    for line in open(os.path.join(ROOT, ".dockerignore")):
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line.rstrip("/"))
    return pats


def ignored(path, patterns):
    path = path.rstrip("/")
    for pat in patterns:
        if pat.startswith("**/"):
            tail = pat[3:]
            parts = path.split("/")
            if any(fnmatch.fnmatch("/".join(parts[i:]), tail) for i in range(len(parts))):
                return True
            if any(fnmatch.fnmatch(p, tail) for p in parts):
                return True
        elif path == pat or path.startswith(pat + "/") or fnmatch.fnmatch(path, pat):
            return True
    return False


@pytest.mark.parametrize("path", CONTAINERFILES, ids=os.path.basename)
def test_containerfile_is_well_formed(path):
    stages = []
    patterns = dockerignore_patterns()
    for ins in instructions(path):
        keyword = ins.split()[0].upper()
        assert keyword in KEYWORDS, f"{path}: unknown instruction {ins!r}"
        if keyword == "FROM":
            m = re.search(r"\sAS\s+(\S+)$", ins, re.I)
            if m:
                stages.append(m.group(1))
        if keyword in ("COPY", "ADD"):
            args = [a for a in shlex.split(ins)[1:] if not a.startswith("--chown")]
            src_from = [a for a in args if a.startswith("--from=")]
            sources = [a for a in args if not a.startswith("--")][:-1]
            if src_from:
                ref = src_from[0].split("=", 1)[1]
                assert ref in stages or "/" in ref or ":" in ref, f"{path}: unknown stage {ref}"
                continue
            for src in sources:
                assert os.path.exists(os.path.join(ROOT, src)), f"{path}: missing {src}"
                assert not ignored(src, patterns), f"{path}: {src} is excluded by .dockerignore"


def test_dockerignore_drops_what_the_spec_lists():
    pats = dockerignore_patterns()
    for path in (".git", "node_modules", "relay/node_modules", ".venv", "venv",
                 "agent/__pycache__", "agent/x.pyc", ".pytest_cache", ".ruff_cache",
                 "eval", "docs", "tests", "deploy/headroom-assets", "relay/dist"):  # fmt: skip
        assert ignored(path, pats), f"{path} should be ignored"
    for path in ("agent", "ingest", "scripts", "skills", "conventions", "pyproject.toml",
                 "uv.lock", "relay/relay.ts", "AGENTS.md"):  # fmt: skip
        assert not ignored(path, pats), f"{path} must stay in the build context"


def test_relay_cmd_matches_esbuild_outputs():
    build = json.load(open(os.path.join(ROOT, "relay", "package.json")))["scripts"]["build"]
    entries = re.findall(r"(\w+)\.ts", build.split("--")[0])
    emitted = {f"dist/{e}.js" for e in entries}
    cmd = [i for i in instructions(os.path.join(ROOT, "deploy", "Containerfile.relay"))
           if i.startswith("CMD")][-1]  # fmt: skip
    assert json.loads(cmd[3:].strip())[0] in emitted
    compose = open(os.path.join(ROOT, "deploy", "compose.yaml")).read()
    cdc = re.search(r"#\s*command:\s*(\[.*?\])", compose).group(1)
    assert json.loads(cdc)[0] in emitted


def test_headroom_image_installs_from_the_lock():
    """Review R1: no floating pip install; the locked group with the proxy extra."""
    text = open(os.path.join(ROOT, "deploy", "Containerfile.headroom")).read()
    steps = instructions(os.path.join(ROOT, "deploy", "Containerfile.headroom"))
    assert not any("pip install" in s for s in steps), "bare pip install in the headroom image"
    assert "COPY pyproject.toml uv.lock ./" in steps
    sync = next(s for s in steps if s.startswith("RUN uv sync"))
    for flag in ("--frozen", "--no-install-project", "--only-group headroom-proxy"):
        assert flag in sync, flag
    assert 'CMD ["headroom", "proxy"]' in steps
    assert "useradd --create-home --uid 10001 headroom" in text
    for var in ("HEADROOM_UPDATE_CHECK=off", "HF_HUB_OFFLINE=1", "ORT_STRATEGY=system",
                "ORT_LIB_LOCATION=/assets/ort", "HF_HOME=/assets/hf", "HEADROOM_HOST=0.0.0.0",
                "HEADROOM_PORT=8787"):  # fmt: skip
        assert var in text, var


def test_image_size_targets_follow_the_evidence_and_the_docs_agree():
    """Review R6: agent 850, ingest 450, relay 160 (headroom per R1)."""
    script = open(os.path.join(ROOT, "scripts", "image_sizes.sh")).read()
    targets = dict(re.findall(r"^check (\w+) (\d+)$", script, re.M))
    assert targets == {"agent": "850", "ingest": "450", "relay": "160", "headroom": "760"}
    for doc in ("README.md", os.path.join("docs", "RUNBOOK.md")):
        text = " ".join(open(os.path.join(ROOT, doc)).read().split())
        for image, mb in targets.items():
            assert re.search(rf"{image}( image)? ≤ {mb} MB", text), (doc, image, mb)
    state = open(os.path.join(ROOT, "docs", "loop", "STATE.md")).read()
    assert "agent ≤ 850, ingest ≤ 450, relay ≤ 160, headroom ≤ 760 MB" in state
    # the gate tools stay in the agent image
    agent = open(os.path.join(ROOT, "deploy", "Containerfile.agent")).read()
    assert "--group agent --group gate" in agent
