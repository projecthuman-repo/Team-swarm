"""Review R5 — host reachability and spine isolation.

Published ports bind to loopback by default; services that publish a port
from the internal-only network get a second, non-internal network only
through the HOSTPORTS=1 override; agents and the relay never leave
`swarm-internal`. What the runtime really does is proven on the runtime host
by scripts/lite_smoke.sh (`make verify-integration`).
"""

import copy
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIND = "${SWARM_BIND_ADDR:-127.0.0.1}:"
PAIRS = [
    ("compose.yaml", "compose.hostports.yaml"),
    ("compose.lite.yaml", "compose.hostports.lite.yaml"),
]


def load(name):
    with open(os.path.join(ROOT, "deploy", name)) as f:
        return yaml.safe_load(f)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def merged(base, override):
    """Compose merge for what these files use: networks are appended per service."""
    out = copy.deepcopy(load(base))
    over = load(override)
    out["networks"] = {**out["networks"], **over["networks"]}
    for name, svc in over["services"].items():
        assert name in out["services"], f"{override} names {name}, which {base} does not define"
        assert set(svc) == {"networks"}, f"{override} changes more than networks for {name}"
        out["services"][name]["networks"] = out["services"][name]["networks"] + svc["networks"]
    return out


def internal_only(compose):
    internal = {n for n, cfg in compose["networks"].items() if (cfg or {}).get("internal")}
    return {
        name for name, svc in compose["services"].items()
        if svc.get("ports") and set(svc["networks"]) <= internal
    }  # fmt: skip


@pytest.mark.parametrize("name", ["compose.yaml", "compose.lite.yaml"])
def test_every_published_port_is_loopback_bound_by_default(name):
    ports = [p for svc in load(name)["services"].values() for p in svc.get("ports", [])]
    assert ports
    for port in ports:
        assert port.startswith(BIND), f"{name}: {port} binds all interfaces"
        assert port[len(BIND) :].count(":") == 1  # HOST:CONTAINER after the address


def test_default_has_the_internal_only_publishers_the_review_found():
    assert internal_only(load("compose.yaml")) == {
        "nats", "postgres", "apicurio", "valkey", "seaweedfs", "openbao", "headroom", "qdrant",
        "opensearch", "langfuse", "jaeger", "prometheus", "perses", "openobserve",
    }  # fmt: skip
    assert internal_only(load("compose.lite.yaml")) == {"nats", "postgres", "valkey", "openbao"}


@pytest.mark.parametrize("base,override", PAIRS)
def test_with_the_override_no_port_publisher_is_internal_only(base, override):
    assert internal_only(merged(base, override)) == set()
    # …and the override names exactly the services that needed it — no more, no fewer
    assert set(load(override)["services"]) == internal_only(load(base))
    net = load(override)["networks"]["swarm-host"]
    assert net == {"external": True, "name": "${SWARM_HOST_NET:-swarm-host}"}


def test_agents_and_relay_stay_on_the_internal_network_in_every_combination():
    for compose in (load("compose.yaml"), merged(*PAIRS[0])):
        isolated = [n for n in compose["services"] if n.startswith("agent-") or n == "relay"]
        assert len(isolated) == 5, isolated  # four role agents + the relay
        for name in isolated:
            assert compose["services"][name]["networks"] == ["swarm-internal"], name
            assert not compose["services"][name].get("ports"), name
        assert compose["networks"]["swarm-internal"] == {"internal": True}
    for override in ("compose.hostports.yaml", "compose.hostports.lite.yaml"):
        names = set(load(override)["services"])
        assert not [n for n in names if n.startswith("agent-") or n == "relay"]


def test_network_script_disables_nat_per_runtime():
    src = read("scripts", "hostports_network.sh")
    assert "com.docker.network.bridge.enable_ip_masquerade=false" in src  # Docker
    assert "--opt no_default_route=true" in src  # Podman >= 4.6
    assert "network inspect" in src  # idempotent
    assert os.access(os.path.join(ROOT, "scripts", "hostports_network.sh"), os.X_OK)


def _make(*args):
    return subprocess.run(
        ["make", "-n", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_make_includes_the_override_only_with_hostports():
    if shutil.which("make") is None:
        pytest.skip("make not installed")
    plain = _make("up") + _make("up-lite")
    assert "hostports" not in plain
    full = _make("up", "HOSTPORTS=1")
    assert "./scripts/hostports_network.sh" in full
    assert "-f deploy/compose.yaml -f deploy/compose.hostports.yaml up -d" in full
    lite = _make("up-lite", "HOSTPORTS=1")
    assert "-f deploy/compose.lite.yaml -f deploy/compose.hostports.lite.yaml up -d" in lite
    assert full.index("hostports_network.sh") < full.index("up -d")  # network first


def test_lite_smoke_proves_reachability_and_isolation():
    smoke = read("scripts", "lite_smoke.sh")
    assert "for port in 5432 4222 6379 8200; do" in smoke
    up, reach, egress, bootstrap = (
        smoke.index("make --no-print-directory up-lite"),
        smoke.index("exit 3"),
        smoke.index("exit 4"),
        smoke.index("make --no-print-directory bootstrap"),
    )
    assert up < reach < egress < bootstrap  # right after the spine is up
    assert 'timeout 5 bash -c "</dev/tcp/${PROBE%:*}/${PROBE#*:}"' in smoke
    # the probe is first shown to work inside the container, so a missing tool is not "isolated"
    assert "timeout 5 bash -c '</dev/tcp/127.0.0.1/5432'" in smoke
    assert smoke.index("</dev/tcp/127.0.0.1/5432'") < smoke.index("${PROBE%:*}")
    # the swarm services are started with the same file set as the spine
    assert 'compose "${FULL[@]}" --profile swarm up -d --build relay agent-backend' in smoke


def test_verify_integration_tries_the_default_then_hostports():
    modes = read("scripts", "lite_smoke_modes.sh")
    first, second = (
        modes.index("HOSTPORTS=0 ./scripts/lite_smoke.sh"),
        modes.index("HOSTPORTS=1 ./scripts/lite_smoke.sh"),
    )
    assert first < modes.index('[ "$rc" -ne 3 ]') < second  # only exit 3 triggers the retry
    assert "host network mode: default" in modes and "host network mode: HOSTPORTS=1" in modes
    assert "./scripts/lite_smoke_modes.sh" in read("Makefile").split("\nverify-integration:")[1]
    assert "HOST_MODE_FILE" in read("scripts", "vi_summary.sh")


def test_bind_address_and_hostports_are_documented():
    env = read(".env.example")
    assert "SWARM_BIND_ADDR" in env and "0.0.0.0" in env and "HOSTPORTS" in env
    assert "HOSTPORTS=1" in read("docs", "RUNBOOK.md") and "SWARM_BIND_ADDR" in read("README.md")
