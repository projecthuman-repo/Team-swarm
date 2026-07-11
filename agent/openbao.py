"""OpenBao secret leasing (Hard Rule 7): secrets are leased, short-TTL,
and never baked into images or env files.

Tier A: `openbao server -dev` from deploy/compose.yaml; seed with
    bao kv put secret/swarm/forgejo token=<bot-token>
Tier B: the openbao-k8s agent injector renders the secret into the pod
    (see deploy/k8s/agent-backend.yaml annotations); in that case we read
    the injected file instead of calling the API.
"""

from __future__ import annotations

import os

import requests

OPENBAO_ADDR = os.environ.get("OPENBAO_ADDR", "http://localhost:8200")
OPENBAO_TOKEN = os.environ.get("OPENBAO_TOKEN", "dev-root")
INJECTED_DIR = os.environ.get("OPENBAO_INJECTED_DIR", "/bao/secrets")


def lease_secret(name: str, field: str = "token") -> str:
    """Return the secret value for secret/data/swarm/<name>.

    Prefers the injector-rendered file (Tier B pods), falls back to the
    KV-v2 HTTP API (Tier A / dev). The value is returned to the caller and
    never written to disk by us.
    """
    injected = os.path.join(INJECTED_DIR, name)
    if os.path.exists(injected):
        with open(injected) as f:
            for line in f:
                # injector template renders: export FORGEJO_TOKEN="..."
                if "=" in line:
                    return line.split("=", 1)[1].strip().strip('"')

    resp = requests.get(
        f"{OPENBAO_ADDR}/v1/secret/data/swarm/{name}",
        headers={"X-Vault-Token": OPENBAO_TOKEN},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["data"][field]
