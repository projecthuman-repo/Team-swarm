"""Tiny FastAPI health + metrics endpoint served alongside the consumer loop.

`/healthz` — liveness/readiness. `/metrics` (Lean & Precise L3.2) — the SLA
gauges of `agent/sla_metrics.py` in Prometheus text format, refreshed every
METRICS_REFRESH_S (default 60) by a background task. Unknown values are NaN.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os

import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from agent import sla_metrics

log = logging.getLogger("health")
METRICS_REFRESH_S = int(os.environ.get("METRICS_REFRESH_S", "60"))


class SlaGauges:
    """One registry per worker; values set by refresh()."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.gauges = {
            name: Gauge(name, doc, registry=self.registry)
            for name, doc in sla_metrics.GAUGES.items()
        }
        for gauge in self.gauges.values():
            gauge.set(math.nan)

    async def refresh(self, pool, stats: dict | None = None) -> None:
        values = await sla_metrics.collect(pool, 24, stats)
        for name, value in values.items():
            self.gauges[name].set(math.nan if value is None else float(value))

    def render(self) -> bytes:
        return generate_latest(self.registry)


async def refresh_forever(gauges: SlaGauges, pool) -> None:
    while True:
        try:
            stats = await asyncio.to_thread(sla_metrics.fetch_headroom_stats)
            await gauges.refresh(pool, stats)
        except Exception as err:  # metrics must never take the worker down
            log.warning("SLA metrics refresh failed: %s", err)
        await asyncio.sleep(METRICS_REFRESH_S)


def build_app(worker_id: str, role: str, gauges: SlaGauges | None = None) -> FastAPI:
    app = FastAPI(title="swarm-agent", docs_url=None, redoc_url=None)
    gauges = gauges or SlaGauges()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "worker": worker_id, "role": role}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(gauges.render(), media_type=CONTENT_TYPE_LATEST)

    return app


async def serve_health(worker_id: str, role: str, pool=None) -> None:
    port = int(os.environ.get("HEALTH_PORT", "8080"))
    gauges = SlaGauges()
    refresher = asyncio.create_task(refresh_forever(gauges, pool)) if pool is not None else None
    config = uvicorn.Config(
        build_app(worker_id, role, gauges),
        # bind-all is required for the k8s readiness probe; the pod network
        # is internal-only (default-deny egress, ambient mTLS)
        host="0.0.0.0",  # nosec B104
        port=port,
        log_level="warning",
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        if refresher is not None:
            refresher.cancel()
