"""Tiny FastAPI health endpoint served alongside the consumer loop."""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI


def build_app(worker_id: str, role: str) -> FastAPI:
    app = FastAPI(title="swarm-agent", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "worker": worker_id, "role": role}

    return app


async def serve_health(worker_id: str, role: str) -> None:
    port = int(os.environ.get("HEALTH_PORT", "8080"))
    config = uvicorn.Config(
        build_app(worker_id, role),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )
    await uvicorn.Server(config).serve()
