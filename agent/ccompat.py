"""Confluent wire-compatibility option (runbook §3 note).

Apicurio ships no first-class Python SerDes; in-swarm messaging uses the
generated Protobuf classes directly (the baseline everywhere in this
repo). If you must interoperate with Confluent-framed consumers/producers,
point confluent-kafka's Protobuf SerDes at Apicurio's ccompat endpoint —
this module builds them correctly configured.

Server-side, disable Apicurio's custom headers so IDs travel in the
Confluent wire format: apicurio.registry.headers.enabled=false.

    pip install -r requirements-ccompat.txt

Budget time to validate wire-format compatibility (the docs' single
biggest integration risk).
"""

from __future__ import annotations

import os

APICURIO_URL = os.environ.get("APICURIO_URL", "http://localhost:8081")


def ccompat_registry_url() -> str:
    """Apicurio's Confluent-compatible registry endpoint (v7 API)."""
    return f"{APICURIO_URL.rstrip('/')}/apis/ccompat/v7"


def make_serializer(msg_type, subject_suffix: str = "-value"):
    """ProtobufSerializer wired to Apicurio ccompat (for Kafka producers)."""
    try:
        from confluent_kafka.schema_registry import SchemaRegistryClient
        from confluent_kafka.schema_registry.protobuf import ProtobufSerializer
    except ImportError as err:
        raise SystemExit(
            "confluent-kafka is optional: pip install -r requirements-ccompat.txt"
        ) from err

    client = SchemaRegistryClient({"url": ccompat_registry_url()})
    return ProtobufSerializer(
        msg_type,
        client,
        {"use.deprecated.format": False},
    )


def make_deserializer(msg_type):
    """ProtobufDeserializer for the matching wire format."""
    try:
        from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer
    except ImportError as err:
        raise SystemExit(
            "confluent-kafka is optional: pip install -r requirements-ccompat.txt"
        ) from err

    return ProtobufDeserializer(msg_type, {"use.deprecated.format": False})
