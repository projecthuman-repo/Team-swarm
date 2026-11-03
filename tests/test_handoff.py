"""L0.3 — cross-process hand-off through Valkey.

A bundle written through one shared_context instance must be readable by a
verifier in a FRESH interpreter (`python -m agent.verifier <task_id>`). The
test runs a minimal RESP server (fake Valkey) and a fake OpenAI-compatible
endpoint; the fake model answers PASS only when the bundle marker reached
the request, so a PASS proves the bundle crossed the process boundary.
"""

import json
import os
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent import shared_context, verifier

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MARKER = "handoff-marker-7f3a"


class _FakeValkey(socketserver.StreamRequestHandler):
    """Just enough RESP2 for PING / SET (EX) / GET; +OK to anything else
    (e.g. the client's CLIENT SETINFO on connect)."""

    store: dict = {}

    def _read_command(self):
        header = self.rfile.readline()
        if not header:
            return None
        count = int(header[1:])
        args = []
        for _ in range(count):
            length = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(length + 2)[:-2])
        return args

    def handle(self):
        while True:
            args = self._read_command()
            if args is None:
                return
            cmd = args[0].upper()
            if cmd == b"PING":
                self.wfile.write(b"+PONG\r\n")
            elif cmd == b"SET":
                self.store[args[1]] = args[2]
                self.wfile.write(b"+OK\r\n")
            elif cmd == b"GET":
                val = self.store.get(args[1])
                if val is None:
                    self.wfile.write(b"$-1\r\n")
                else:
                    self.wfile.write(b"$%d\r\n%s\r\n" % (len(val), val))
            else:
                self.wfile.write(b"+OK\r\n")


class _FakeModel(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        self.seen.append(MARKER in prompt)
        verdict = "VERDICT: PASS" if MARKER in prompt else "VERDICT: BLOCK — no bundle"
        payload = json.dumps({"choices": [{"message": {"content": f"ok\n{verdict}"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def servers():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    valkey_srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeValkey)
    model_srv = HTTPServer(("127.0.0.1", 0), _FakeModel)
    _FakeValkey.store = {}
    _FakeModel.seen = []
    for srv in (valkey_srv, model_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield valkey_srv.server_address[1], model_srv.server_address[1]
    valkey_srv.shutdown()
    model_srv.shutdown()


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("VALKEY_URL", "HEADROOM_ENABLED")}
    env.update(PYTHONPATH=ROOT, **extra)
    return env


def test_bundle_crosses_process_boundary(servers):
    valkey_port, model_port = servers
    env = _env(
        VALKEY_URL=f"valkey://127.0.0.1:{valkey_port}",
        VERIFIER_API_BASE=f"http://127.0.0.1:{model_port}/v1",
    )
    writer = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent.shared_context import put_handoff; "
            f"print(put_handoff('task-42', 'diff --git a/x b/x\\n+{MARKER}', 'spec'))",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert writer.returncode == 0, writer.stderr
    assert writer.stdout.strip() == "handoff:task-42"
    assert b"handoff:task-42" in _FakeValkey.store  # landed in Valkey, not a dict

    reader = subprocess.run(
        [sys.executable, "-m", "agent.verifier", "task-42"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert reader.returncode == 0, reader.stdout + reader.stderr
    assert json.loads(reader.stdout)["verdict"] == "PASS"
    assert _FakeModel.seen and all(_FakeModel.seen)  # every lens got the bundle


def test_missing_bundle_is_reported(servers):
    valkey_port, model_port = servers
    env = _env(
        VALKEY_URL=f"valkey://127.0.0.1:{valkey_port}",
        VERIFIER_API_BASE=f"http://127.0.0.1:{model_port}/v1",
    )
    reader = subprocess.run(
        [sys.executable, "-m", "agent.verifier", "task-absent"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert reader.returncode != 0
    assert "no hand-off bundle for task task-absent" in reader.stderr


def test_unreachable_valkey_degrades_to_memory_with_warning():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent import shared_context as s; s.put_handoff('t','d','sp'); "
            "print(s._valkey is None, s.get_handoff('t') is not None)",
        ],
        cwd=ROOT,
        env=_env(VALKEY_URL="valkey://127.0.0.1:9"),  # closed port
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["True", "True"]
    assert "unreachable" in probe.stderr


def test_unset_valkey_url_uses_memory_in_process(monkeypatch):
    if os.environ.get("VALKEY_URL"):
        pytest.skip("VALKEY_URL set in this environment")
    assert shared_context._valkey is None
    shared_context.put_handoff("t-mem", "the-diff", "the-spec")
    bundle = shared_context.get_handoff("t-mem")
    assert "the-diff" in bundle and "the-spec" in bundle


def test_verifier_endpoint_defaults(monkeypatch):
    monkeypatch.setenv("HEADROOM_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_URL", "http://headroom:8787/v1")
    assert verifier._default_api_base() == "http://headroom:8787/v1"
    monkeypatch.setenv("HEADROOM_ENABLED", "0")
    monkeypatch.setenv("OPENAI_API_BASE", "http://vllm:8000/v1")
    assert verifier._default_api_base() == "http://vllm:8000/v1"
