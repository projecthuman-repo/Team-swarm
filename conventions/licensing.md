# Licensing — the three-zone model (Lean & Precise L3.1)

The swarm separates **what it talks to**, **what its code links**, and
**what it ships**. Each zone has its own rule; the license gate
(`.forgejo/scripts/license_gate.py`) enforces B and C on every PR and is
fail-closed for anything that ships.

| zone | what | rule | gate |
|---|---|---|---|
| **A** | network services the swarm calls over a socket, each in its own container (no linking, no redistribution of modified code) | any OSI-approved license | `--zone A` prints the table below, exits 0 |
| **B** | everything linked or imported by swarm code anywhere in the repo (runtime, dev and CI tooling, every lockfile) | no AGPL, SSPL, BUSL/Business Source, Commons Clause | `--zone B` (default path `dir:.`) — denylist |
| **C** | the contents of the images the swarm builds and the code it ships: `agent/ ingest/ relay/ skills/ scripts/`, the Python closure (`pyproject.toml`, `uv.lock`) and the relay's npm closure (`relay/package-lock.json`) | permissive only: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC, 0BSD (+ PostgreSQL, PSF, Unlicense, CC0, BlueOak equivalents); anything else blocks | `--zone C` (default) — fail-closed allowlist |

**Image contents are Zone C.** Every image built from `deploy/Containerfile.*`
(agent, ingest, relay, headroom) ships only Zone C application code and
dependencies. The OS base layers they start from (`python:3.12-slim`,
`node:22-slim` builder, `gcr.io/distroless/nodejs22-debian12`) are
distribution aggregates under their own package licenses (glibc LGPL,
bash/coreutils GPL in Debian slim); they are platform, not swarm code, and
no swarm code links against a GPL library from them.

## Services (Zone A)

Every compose service and Kubernetes image the swarm runs but does not build.

| service | image | license | zone | notes |
|---|---|---|---|---|
| NATS JetStream | `nats:2.12` | Apache-2.0 | A | message bus |
| Postgres + pgvector | `pgvector/pgvector:pg16` | PostgreSQL | A | source of truth, vector memory |
| Apicurio Registry | `apicurio/apicurio-registry:3.3.0` | Apache-2.0 | A | Protobuf schema registry |
| Valkey | `valkey/valkey:9.0` | BSD-3-Clause | A | locks, budgets, hand-off |
| SeaweedFS | `chrislusf/seaweedfs:latest` | Apache-2.0 | A | artifact store |
| OpenBao | `openbao/openbao:2.5.0` | MPL-2.0 | A | leased secrets |
| Ollama | `ollama/ollama:latest` | MIT | A | Tier A inference |
| vLLM | `vllm/vllm-openai:latest` | Apache-2.0 | A | `--profile vllm`, Tier B |
| LiteLLM | `ghcr.io/berriai/litellm:main-latest` | MIT | A | `--profile litellm`; core only, `enterprise/` unused |
| Qdrant | `qdrant/qdrant:latest` | Apache-2.0 | A | `--profile qdrant` |
| Forgejo | `codeberg.org/forgejo/forgejo:11` | GPL-3.0-or-later | A | `--profile forgejo`; isolated service, API over HTTP only |
| OpenSearch | `opensearchproject/opensearch:2` | Apache-2.0 | A | `--profile observability` (logs) |
| Langfuse | `langfuse/langfuse:2` | MIT | A | `--profile observability` (LLM traces); `ee/` unused |
| Jaeger | `jaegertracing/jaeger:2.22.0` | Apache-2.0 | A | `--profile observability` (traces, L3.2) |
| Prometheus | `prom/prometheus:v3.15.1` | Apache-2.0 | A | `--profile observability` (metrics, L3.2) |
| Perses | `persesdev/perses:v0.55.1` | Apache-2.0 | A | `--profile observability` (canonical dashboards, L3.2) |
| OpenObserve | `public.ecr.aws/zinclabs/openobserve:latest` | AGPL-3.0 | A | `--profile openobserve`; optional swap, unmodified, over HTTP |
| Grafana | `grafana/grafana` (operator-run) | AGPL-3.0 | A | option: import `deploy/observability/grafana-sla.json`; unmodified, over HTTP |

## Built images (Zone C contents)

| image | built from | contents rule |
|---|---|---|
| swarm-agent | `deploy/Containerfile.agent` | Zone C — `uv.lock` agent group closure + `agent/ ingest/ skills/ scripts/ conventions/` |
| swarm-ingest | `deploy/Containerfile.ingest` | Zone C — `uv.lock` ingest group closure + `ingest/` |
| swarm-relay | `deploy/Containerfile.relay` | Zone C — esbuild bundle of `relay/` + `relay/package-lock.json` closure |
| swarm-headroom | `deploy/Containerfile.headroom` | Zone C — headroom-ai (Apache-2.0) closure |

## Dependencies added by Lean & Precise v4.1.1

Direct additions only — the full closure is what the gate scans (syft over
`uv.lock` / `relay/package-lock.json`). Licences read from the installed
package metadata unless noted.

| dependency | version | license | zone | where |
|---|---|---|---|---|
| fastembed | 0.9.1 | Apache-2.0 | C | extra `embeddings` (L2.3) |
| ↳ onnxruntime | 1.31.1 | MIT | C | fastembed runtime |
| ↳ tokenizers | (lock) | Apache-2.0 | C | fastembed runtime |
| ↳ mmh3 / loguru | (lock) | MIT | C | fastembed runtime |
| ↳ py-rust-stemmers | 0.1.8 | MIT (upstream repo; the wheel metadata declares none — syft reports NOASSERTION) | C | fastembed runtime |
| tree-sitter-language-pack | 0.13.0 | MIT OR Apache-2.0 | C | already in the agent closure via aider; used by L2.4 |
| prometheus-client | 0.26.0 | Apache-2.0 | C | base deps, `/metrics` (L3.2) |
| esbuild | ^0.25.12 | MIT | B | relay builder stage only — not in the runtime image (L1.4) |
| pre-commit | 4.x | MIT | B | dev group (L1.3) |
| pre-commit hooks: ruff-pre-commit, uv-pre-commit, pre-commit-hooks | v0.17.3 / 0.11.32 / v6.0.0 | MIT / MIT OR Apache-2.0 / MIT | B | developer machines only |
| uv | 0.11 (`ghcr.io/astral-sh/uv:0.11`) | MIT OR Apache-2.0 | B | builder stages only — copied out of the runtime images |
| `gcr.io/distroless/nodejs22-debian12:nonroot` | — | Apache-2.0 (distroless) + Debian package licenses | base layer | relay runtime base (L1.4) |
| `node:22-slim` | — | MIT (Node.js) + Debian package licenses | base layer | relay builder only |

Adding a service: give it a row above with its SPDX license and zone. A
service under AGPL/SSPL/BUSL stays Zone A only while it runs unmodified in
its own container and nothing in the repo imports its client library under
that license (Zone B catches that).
