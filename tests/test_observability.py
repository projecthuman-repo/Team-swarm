"""L3.2 — /metrics SLA gauges, Jaeger spans, Prometheus + Perses config."""

import json
import math
import os
import socket
import threading
import time
import uuid

import pytest
import requests
import uvicorn
import yaml

from agent import health, sla_metrics, tracing

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FakePool:
    """Answers each SLA query by its SQL text."""

    async def fetchrow(self, sql, *args):
        if "AS done" in sql:
            return {"total": 10, "done": 8}
        if "p95_s" in sql:
            return {"avg_pickup": None, "p95_s": 42.5}
        if "measured" in sql:
            return {"green": 3, "measured": 4, "avg_repairs": 0.5}
        if "gate_stage1_ms" in sql:
            return {"s1": 1500.0, "s2": None}
        if "FROM outbox" in sql and "pending" in sql:
            return {"n": 7, "pending": 1}
        if "snowball" in sql:
            return {"dlq": 2, "snowball": 1}
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        assert "tokens_spent" in sql
        return 12345.0


EXPECTED = {
    "swarm_tasks_done_24h_ratio": 0.8,
    "swarm_pickup_latency_p95_seconds": 42.5,
    "swarm_first_pass_green_ratio": 0.75,
    "swarm_tokens_per_merged_pr": 12345.0,
    "swarm_headroom_savings_ratio": 0.25,
    "swarm_dlq_depth": 2.0,
    "swarm_snowball_total": 1.0,
    "swarm_gate_stage1_ms_median": 1500.0,
    "swarm_outbox_rows": 7.0,
}


def parse(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_metrics_endpoint_renders_every_gauge():
    gauges = health.SlaGauges()
    assert all(math.isnan(v) for v in parse(gauges.render().decode()).values())  # before refresh
    await gauges.refresh(FakePool(), stats={"tokens_pre": 1000, "tokens_post": 750})
    assert set(sla_metrics.GAUGES) == set(EXPECTED)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(health.build_app("w-1", "backend", gauges), port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        resp = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
        values = parse(resp.text)
        for name, want in EXPECTED.items():
            assert values[name] == pytest.approx(want), name
        assert requests.get(f"http://127.0.0.1:{port}/healthz", timeout=5).json()["ok"] is True
    finally:
        server.should_exit = True
        thread.join(5)


async def test_no_data_is_nan():
    class Empty(FakePool):
        async def fetchrow(self, sql, *args):
            row = await super().fetchrow(sql, *args)
            return {k: (0 if k in ("total", "done", "measured", "green") else None)
                    for k in row} | ({"n": 0, "pending": 0} if "pending" in sql else {}) | (
                        {"dlq": 0, "snowball": 0} if "snowball" in sql else {})  # fmt: skip

        async def fetchval(self, sql, *args):
            return None

    gauges = health.SlaGauges()
    await gauges.refresh(Empty(), stats=None)
    values = parse(gauges.render().decode())
    assert math.isnan(values["swarm_tasks_done_24h_ratio"])
    assert math.isnan(values["swarm_headroom_savings_ratio"])
    assert values["swarm_outbox_rows"] == 0.0


async def test_collect_in_real_postgres(pg):
    t1, t2, t3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await pg.execute(
        "INSERT INTO tasks(task_id, role, status, budget_tokens, first_pass_green, "
        "gate_stage1_ms, tokens_spent) VALUES "
        "($1,'backend','done',1,true,1000,3000), ($2,'backend','done',1,false,3000,5000), "
        "($3,'backend','failed',1,null,null,null)",
        t1, t2, t3,
    )  # fmt: skip
    await pg.execute(
        "INSERT INTO outbox(subject, msg_id, payload) VALUES "
        "('swarm.events.agent', 'e1', convert_to('x reason=snowball y', 'UTF8')), "
        "('swarm.tasks.backend', $1, '\\x00')",
        str(t1),
    )
    got = await sla_metrics.collect(pg, 24, stats=None)
    assert got["swarm_tasks_done_24h_ratio"] == pytest.approx(2 / 3)
    assert got["swarm_first_pass_green_ratio"] == pytest.approx(0.5)
    assert got["swarm_tokens_per_merged_pr"] == pytest.approx(4000)
    assert got["swarm_gate_stage1_ms_median"] == pytest.approx(2000)
    assert got["swarm_dlq_depth"] == 1 and got["swarm_snowball_total"] == 1
    assert got["swarm_outbox_rows"] == 2
    assert got["swarm_headroom_savings_ratio"] is None


def test_emit_trace_sends_a_span_with_the_task_trace_id(monkeypatch):
    posts = []
    monkeypatch.setattr(tracing, "OTLP_ENDPOINT", "http://jaeger:4318")
    monkeypatch.setattr(tracing.requests, "post", lambda url, **kw: posts.append((url, kw)))
    task_id = str(uuid.uuid4())
    tracing.emit_trace("claimed", task_id, "backend", detail="synthetic")
    urls = [u for u, _ in posts]
    assert "http://jaeger:4318/v1/logs" in urls and "http://jaeger:4318/v1/traces" in urls
    span_body = dict(posts)["http://jaeger:4318/v1/traces"]["json"]
    span = span_body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == task_id.replace("-", "") and span["name"] == "swarm.claimed"


def test_compose_observability_services():
    with open(os.path.join(ROOT, "deploy", "compose.yaml")) as f:
        svc = yaml.safe_load(f)["services"]
    assert svc["jaeger"]["image"] == "jaegertracing/jaeger:2.22.0"
    assert svc["prometheus"]["image"] == "prom/prometheus:v3.15.1"
    assert svc["perses"]["image"] == "persesdev/perses:v0.55.1"
    for name in ("jaeger", "prometheus", "perses"):
        assert svc[name]["profiles"] == ["observability"]
    bind = "${SWARM_BIND_ADDR:-127.0.0.1}:"  # R5: every published port is loopback-bound
    assert {bind + "16686:16686", bind + "4318:4318"} <= set(svc["jaeger"]["ports"])
    assert any("perses-sla.json" in v for v in svc["perses"]["volumes"])
    for v in svc["perses"]["volumes"] + svc["prometheus"]["volumes"]:
        assert os.path.exists(os.path.join(ROOT, "deploy", v.split(":")[0]))
    env = svc["agent-backend"]["environment"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "${OTEL_EXPORTER_OTLP_ENDPOINT-http://jaeger:4318}"


def test_prometheus_config_targets():
    with open(os.path.join(ROOT, "deploy", "observability", "prometheus.yml")) as f:
        cfg = yaml.safe_load(f)
    targets = {t for job in cfg["scrape_configs"] for sc in job["static_configs"]
               for t in sc["targets"]}  # fmt: skip
    assert targets == {
        "agent-backend:8080",
        "agent-frontend:8080",
        "agent-review:8080",
        "agent-triage:8080",
        "relay:9464",
        "headroom:8787",
    }  # fmt: skip — headroom: review R1


def test_perses_dashboard_covers_every_gauge_with_sla_thresholds():
    with open(os.path.join(ROOT, "deploy", "observability", "perses-sla.json")) as f:
        dash = json.load(f)
    assert dash["kind"] == "Dashboard"
    assert dash["metadata"] == {"name": "swarm-sla", "project": "swarm"}
    panels = dash["spec"]["panels"]
    queries = {
        p["spec"]["queries"][0]["spec"]["plugin"]["spec"]["query"]: p for p in panels.values()
    }
    assert set(queries) == {f"max({g})" for g in sla_metrics.GAUGES}
    done = queries["max(swarm_tasks_done_24h_ratio)"]["spec"]["plugin"]["spec"]["thresholds"]
    assert done["steps"] == [{"value": 0.7, "color": "#56a64b"}]
    p95 = queries["max(swarm_pickup_latency_p95_seconds)"]["spec"]["plugin"]["spec"]["thresholds"]
    assert p95["steps"] == [{"value": 600, "color": "#e02f44"}]
    refs = {
        i["content"]["$ref"] for layout in dash["spec"]["layouts"] for i in layout["spec"]["items"]
    }
    assert refs == {f"#/spec/panels/{k}" for k in panels}
    with open(os.path.join(ROOT, "deploy", "observability", "perses", "resources.json")) as f:
        kinds = {r["kind"]: r for r in json.load(f)}
    assert kinds["Project"]["metadata"]["name"] == "swarm"
    assert kinds["GlobalDatasource"]["spec"]["plugin"]["kind"] == "PrometheusDatasource"
