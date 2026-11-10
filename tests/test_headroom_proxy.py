"""Review R1 — the Headroom proxy runs from the lock.

`deploy/Containerfile.headroom` used to `pip install headroom-ai` unpinned
and without the `proxy` extra, so `headroom proxy` exited at start-up
("Proxy dependencies not installed … No module named 'fastapi'").

The start-up test needs an environment synced with
`uv sync --frozen --no-install-project --only-group headroom-proxy` — the
exact install of the image's builder stage. `make ci-local` and CI create
it and pass its path in HEADROOM_PROXY_ENV; without that variable the test
is skipped (REQUIRE_HEADROOM_ENV=1 turns the skip into a failure).
"""

import json
import os
import re
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


@pytest.fixture
def headroom_bin():
    env_dir = os.environ.get("HEADROOM_PROXY_ENV")
    binary = os.path.join(env_dir, "bin", "headroom") if env_dir else None
    if not binary or not os.path.exists(binary):
        reason = "HEADROOM_PROXY_ENV is not set to a headroom-proxy environment"
        if os.environ.get("REQUIRE_HEADROOM_ENV") == "1":
            pytest.fail(f"REQUIRE_HEADROOM_ENV=1 but {reason}")
        pytest.skip(f"{reason}; `make ci-local` provides it")
    return binary


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — loopback only
        return resp.status, resp.read().decode()


def test_proxy_starts_from_the_locked_group_and_is_healthy(headroom_bin, tmp_path):
    port = _free_port()
    env = {
        "PATH": os.path.dirname(headroom_bin) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HEADROOM_UPDATE_CHECK": "off",
        "HF_HUB_OFFLINE": "1",
        "HEADROOM_HOST": "127.0.0.1",
        "HEADROOM_PORT": str(port),
    }
    log = open(tmp_path / "proxy.log", "w")
    proc = subprocess.Popen([headroom_bin, "proxy"], env=env, stdout=log, stderr=log, cwd=tmp_path)
    try:
        status, body, deadline = None, "", time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                status, body = _get(f"http://127.0.0.1:{port}/health")
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        output = (tmp_path / "proxy.log").read_text()[-2000:]
        assert status == 200, f"`headroom proxy` gave no /health 200 within 60 s:\n{output}"
        assert json.loads(body)["version"] == "0.31.0"
        # what deploy/observability/prometheus.yml scrapes (job `headroom`)
        m_status, metrics = _get(f"http://127.0.0.1:{port}/metrics")
        assert m_status == 200 and "headroom_requests_total" in metrics
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_proxy_forwards_to_the_upstream_the_deployments_configure(headroom_bin, tmp_path):
    """Review of R1: /health alone did not show that the sidecar forwards anywhere.

    The 0.31.0 proxy takes its OpenAI upstream from OPENAI_TARGET_API_URL;
    with only OPENAI_BASE_URL set it forwards to api.openai.com — on the
    no-egress network that is a 502 for every engine call.
    """
    import http.server
    import threading

    hits = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — http.server's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hits.append(self.path)
            body = json.dumps({
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()  # fmt: skip
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    services = yaml.safe_load(read("deploy", "compose.yaml"))["services"]
    configured = services["headroom"]["environment"]
    assert configured["OPENAI_TARGET_API_URL"] == "http://ollama:11434/v1"
    assert "OPENAI_BASE_URL" not in configured
    k8s = read("deploy", "k8s", "headroom.yaml")
    assert "{ name: OPENAI_TARGET_API_URL, value: http://vllm:8000/v1 }" in k8s
    assert "name: OPENAI_BASE_URL" not in k8s

    port = _free_port()
    env = {
        "PATH": os.path.dirname(headroom_bin) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HEADROOM_UPDATE_CHECK": "off",
        "HF_HUB_OFFLINE": "1",
        "HEADROOM_HOST": "127.0.0.1",
        "HEADROOM_PORT": str(port),
        # the variable the deployments set, pointed at the fake upstream
        "OPENAI_TARGET_API_URL": f"http://127.0.0.1:{upstream.server_address[1]}/v1",
    }
    log = open(tmp_path / "proxy.log", "w")
    proc = subprocess.Popen([headroom_bin, "proxy"], env=env, stdout=log, stderr=log, cwd=tmp_path)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                _get(f"http://127.0.0.1:{port}/health")
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        request = urllib.request.Request(  # noqa: S310 — loopback only
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(
                {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
            ).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer none"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        output = (tmp_path / "proxy.log").read_text()[-2000:]
        assert status == 200, f"the proxy did not forward (HTTP {status}):\n{output}"
        assert hits == ["/v1/chat/completions"], output
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        upstream.shutdown()


def test_headroom_proxy_group_is_locked_and_conflicts_with_the_agent():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)
    assert project["dependency-groups"]["headroom-proxy"] == ["headroom-ai[proxy]==0.31.0"]
    conflicts = [sorted(c["group"] for c in pair) for pair in project["tool"]["uv"]["conflicts"]]
    assert ["agent", "headroom-proxy"] in conflicts and ["agent", "ingest"] in conflicts
    pins = read("requirements-headroom-proxy.txt")
    assert pins.startswith("# GENERATED by make lock")
    for needed in ("headroom-ai==0.31.0", "fastapi==", "uvicorn=="):
        assert re.search(rf"^{re.escape(needed)}", pins, re.M), needed
    assert not re.search(r"^(torch|nvidia-(?!ml-py))", pins, re.M)


def test_ci_gate_and_tools_install_the_locked_headroom():
    for path in ((".forgejo", "workflows", "ci.yaml"), ("scripts", "ci_local.sh")):
        src = read(*path)
        assert "--only-group headroom-proxy" in src and "--frozen" in src, path
        assert "pip install" not in src.split("headroom environment from uv.lock")[1], path
    tools = read("Makefile").split("\ntools:")[1].split("\n\n")[0]
    assert "--constraints requirements-headroom-proxy.txt headroom-ai==0.31.0" in tools


def test_prometheus_scrapes_the_proxy():
    cfg = yaml.safe_load(read("deploy", "observability", "prometheus.yml"))
    job = next(j for j in cfg["scrape_configs"] if j["job_name"] == "headroom")
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["headroom:8787"]


def test_headroom_image_has_a_size_target_and_a_start_up_check():
    assert re.search(r"^check headroom \d+$", read("scripts", "image_sizes.sh"), re.M)
    recipe = read("Makefile").split("\nverify-integration:")[1]
    assert "./scripts/headroom_image_smoke.sh" in recipe
