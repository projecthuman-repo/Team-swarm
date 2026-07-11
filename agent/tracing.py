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


def otlp_span_payload(
    name: str, attrs: dict, start_ns: int, end_ns: int, trace_id: str
) -> dict:
    """Minimal OTLP/HTTP JSON traces payload — one GenAI-convention span
    (v4.1 T5.2): steps, LLM calls, and retries each get their own span."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "swarm-agent"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "swarm"},
                        "spans": [
                            {
                                "traceId": trace_id.replace("-", "")[:32].ljust(32, "0"),
                                "spanId": uuid.uuid4().hex[:16],
                                "name": name,
                                "kind": 1,
                                "startTimeUnixNano": str(start_ns),
                                "endTimeUnixNano": str(end_ns),
                                "attributes": [
                                    {"key": k, "value": {"stringValue": str(v)}}
                                    for k, v in attrs.items()
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def emit_span(
    name: str,
    task_id: str,
    attrs: dict | None = None,
    duration_s: float = 0.0,
) -> None:
    """One OTel span per step / LLM call / retry (T5.2). Never raises.

    GenAI-convention attributes callers commonly pass: gen_ai.usage.
    input_tokens / output_tokens, retry.count, headroom.tokens_pre /
    tokens_post / compression_ratio (T1.9), cost estimates.
    """
    if not OTLP_ENDPOINT:
        return
    end_ns = time.time_ns()
    start_ns = end_ns - int(duration_s * 1e9)
    try:
        headers = dict(h.split("=", 1) for h in OTLP_HEADERS.split(",") if "=" in h)
        requests.post(
            f"{OTLP_ENDPOINT.rstrip('/')}/v1/traces",
            json=otlp_span_payload(name, attrs or {}, start_ns, end_ns, task_id),
            headers=headers,
            timeout=5,
        )
    except requests.RequestException as err:
        log.debug("otlp span emit failed: %s", err)


def emit_trace(
    kind: str,
    task_id: str,
    agent: str,
    detail: str = "",
    tokens_spent: int = 0,
    tokens_pre_compression: int = 0,
    tokens_post_compression: int = 0,
) -> None:
    """Send one agent action to every configured sink. Never raises.

    Compression accounting (v4.1 T1.9): tokens_pre/post carry Headroom's
    per-task proxy stats. Budgets keep metering what the engine actually
    sends (post-compression) — the pre/post pair here is observability,
    not billing.
    """
    event = {
        "kind": kind,
        "task_id": task_id,
        "agent": agent,
        "detail": detail[:2000],
        "tokens_spent": tokens_spent,
    }
    if tokens_pre_compression or tokens_post_compression:
        event["headroom.tokens_pre"] = tokens_pre_compression
        event["headroom.tokens_post"] = tokens_post_compression
        event["headroom.compression_ratio"] = round(
            tokens_post_compression / max(tokens_pre_compression, 1), 4
        )
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
