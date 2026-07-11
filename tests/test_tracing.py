"""Tracing fan-out payload builders (Langfuse / OpenSearch / OTLP)."""

from agent.tracing import langfuse_batch, opensearch_doc, otlp_logs_payload

EVENT = {
    "kind": "done",
    "task_id": "t-1",
    "agent": "backend",
    "detail": "branch=feature/t-1",
    "tokens_spent": 4200,
}


def test_langfuse_batch_shape():
    batch = langfuse_batch(EVENT)["batch"]
    assert len(batch) == 1
    assert batch[0]["type"] == "trace-create"
    assert batch[0]["body"]["id"] == "t-1"
    assert "backend" in batch[0]["body"]["tags"]


def test_opensearch_doc_has_timestamp():
    doc = opensearch_doc(EVENT)
    assert doc["@timestamp"] > 0
    assert doc["service"] == "swarm-agent"
    assert doc["tokens_spent"] == 4200


def test_otlp_payload_shape():
    logs = otlp_logs_payload(EVENT)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(logs) == 1
    keys = {a["key"] for a in logs[0]["attributes"]}
    assert {"kind", "task_id", "agent", "tokens_spent"} <= keys
