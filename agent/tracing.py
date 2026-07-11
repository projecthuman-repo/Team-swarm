"""Observability fan-out (runbook §16): every agent action/prompt/diff/
token-count/check-result goes to whichever sinks are configured. All three
are optional and independent; unset env vars disable a sink.

  Langfuse (baseline traces)   LANGFUSE_HOST + LANGFUSE_PUBLIC_KEY/SECRET_KEY
  OpenSearch (baseline logs)   OPENSEARCH_URL   (index: swarm-logs)
  OTLP/HTTP (optional swap)    OTEL_EXPORTER_OTLP_ENDPOINT — feeds
      OpenObserve (single-binary swap) or any OTel collector
      (Laminar / OpenLLMetry-style vendor-neutral instrumentation)

Emission is fire-and-forget with short timeouts: observability must never
fail a task.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import requests

log = logging.getLogger("tracing")

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "")
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTLP_HEADERS = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")


def langfuse_batch(event: dict) -> dict:
    """Langfuse public ingestion API batch for one agent action."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "batch": [
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": now_iso,
                "body": {
                    "id": event["task_id"],
                    "name": f"task-{event['kind']}",
                    "metadata": event,
                    "tags": [event["agent"], event["kind"]],
                },
            }
        ]
    }


def opensearch_doc(event: dict) -> dict:
    return {**event, "@timestamp": int(time.time() * 1000), "service": "swarm-agent"}


def otlp_logs_payload(event: dict) -> dict:
    """Minimal OTLP/HTTP JSON logs payload (OpenObserve / any collector)."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "swarm-agent"},
                        }
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "swarm"},
                        "logRecords": [
                            {
                                "timeUnixNano": str(time.time_ns()),
                                "severityText": "INFO",
                                "body": {"stringValue": f"{event['kind']} {event['task_id']}"},
                                "attributes": [
                                    {"key": k, "value": {"stringValue": str(v)}}
                                    for k, v in event.items()
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def emit_trace(
    kind: str,
    task_id: str,
    agent: str,
    detail: str = "",
    tokens_spent: int = 0,
) -> None:
    """Send one agent action to every configured sink. Never raises."""
    event = {
        "kind": kind,
        "task_id": task_id,
        "agent": agent,
        "detail": detail[:2000],
        "tokens_spent": tokens_spent,
    }
    if LANGFUSE_HOST and LANGFUSE_PUBLIC_KEY:
        try:
            requests.post(
                f"{LANGFUSE_HOST.rstrip('/')}/api/public/ingestion",
                json=langfuse_batch(event),
                auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
                timeout=5,
            )
        except requests.RequestException as err:
            log.debug("langfuse emit failed: %s", err)
    if OPENSEARCH_URL:
        try:
            requests.post(
                f"{OPENSEARCH_URL.rstrip('/')}/swarm-logs/_doc",
                json=opensearch_doc(event),
                timeout=5,
            )
        except requests.RequestException as err:
            log.debug("opensearch emit failed: %s", err)
    if OTLP_ENDPOINT:
        try:
            headers = dict(
                h.split("=", 1) for h in OTLP_HEADERS.split(",") if "=" in h
            )
            requests.post(
                f"{OTLP_ENDPOINT.rstrip('/')}/v1/logs",
                json=otlp_logs_payload(event),
                headers=headers,
                timeout=5,
            )
        except requests.RequestException as err:
            log.debug("otlp emit failed: %s", err)
