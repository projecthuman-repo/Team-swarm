# Team-swarm — Overnight Agent Swarm v4.1.1 "Lean & Precise"

A self-hosted, licence-clean multi-agent coding swarm: autonomous agents
(backend / frontend / review / triage) pick up tasks overnight, write code
with [aider](https://github.com/Aider-AI/aider) + **Ornith-1.0** on local
inference, open PRs on your own Forgejo, and merge to `integration` only
when every CI gate is green. No cloud calls, no agent egress, and everything
that ships is permissively licensed (Apache/MIT/BSD/MPL — a fail-closed gate;
the three licence zones are in [conventions/licensing.md](conventions/licensing.md)).

```
                          ┌─────────────────────────────────────────────┐
                          │  Forgejo (Git + Actions + API auto-merge)    │
                          │  feature/* ──checks──▶ integration (never main)
                          └───────────────▲──────────────┬──────────────┘
                                          │ push/PR      │ webhook (status)
   ┌──────────┐                 ┌─────────┴──────────────▼───────────────┐
   │ Telegram │──outbox write──▶│ Postgres (tasks · outbox · lessons)     │
   │ /issues  │                 │ pgvector (vector memory, writable)      │
   └──────────┘                 └───────────────┬────────────────────────┘
                                                ▼
                              ┌─────────────────────────────┐
                              │ Outbox Relay (TS/Node 22)    │ FOR UPDATE SKIP LOCKED
                              │ the ONLY JetStream publisher │ Nats-Msg-Id dedupe
                              └───────────────┬─────────────┘
                                              ▼
   ┌──────────── NATS JetStream (work-queue + durable pull consumers) ─────────────┐
   │ swarm.tasks.backend  swarm.tasks.frontend  swarm.tasks.review  swarm.tasks.triage
   │ swarm.events.>       SWARM_DLQ (MAX_DELIVERIES advisory)                      │
   └───▲────────────▲───────────────▲───────────────▲──────────────────────────────┘
  ┌────┴───┐   ┌────┴────┐    ┌─────┴────┐    ┌─────┴────┐
  │backend │   │frontend │    │ reviewer │    │ triage   │  Python 3.12 workers
  │ agent  │   │ agent   │    │  agent   │    │  agent   │  running aider (Ornith-1.0)
  └───┬────┘   └────┬────┘    └────┬─────┘    └────┬─────┘  in sandboxed containers
      ▼             ▼              ▼               ▼
  OpenBao (leased secrets)  SeaweedFS (artifacts)  Valkey (locks · budgets)
   OpenSearch (logs) · Jaeger (traces) · Prometheus + Perses (SLA dashboards) · Langfuse
```

## Quickstart (Tier A — one laptop, Podman, no Kubernetes)

Prereqs: [Podman](https://podman.io) (or Docker) with `compose`, Python 3.12,
Node 22 (only if you run the relay on the host), ~10 GB disk, a 6 GB+ GPU
(or patience on CPU).

```bash
git clone <this-repo> && cd Team-swarm

make quickstart     # start nats/postgres/apicurio/valkey/seaweedfs/openbao/ollama,
                    # apply the DB schema, create JetStream streams + consumers,
                    # seed dev secrets, register Protobuf schemas
make models         # pull Ornith-1.0-9B (Q4, fits a 12GB GPU)
make swarm          # build + start the outbox relay and one backend agent
make inject         # inject a synthetic task through the outbox
make logs           # watch it flow: outbox -> relay -> JetStream -> agent
```

### Lite path (smallest footprint)

Four spine services instead of seven — no Apicurio, no SeaweedFS, and Ollama
only when you ask for it. Same images, volumes and networks as the full file
(`deploy/compose.lite.yaml`), so `make up` later just adds the rest.

```bash
make up-lite && make bootstrap && make ollama && make forgejo && make agent ROLE=backend
make relay                # second terminal — the outbox relay is the only JetStream publisher
make inject               # third terminal — one synthetic task
```

`make bootstrap` detects the lite spine and skips Protobuf schema registration
(the `proto-compat` CI job still guards compatibility). `make down-lite` stops
it; `make disk-usage` shows container disk use and `make clean` prunes it
(keeping the swarm's own data volumes).

Run components on the host instead of in containers (nicer for hacking):

```bash
cp .env.example .env
uv sync --frozen --group agent --group dev   # pyproject.toml + uv.lock are the source of truth
#   fallback without uv: pip install -r requirements.txt -r requirements-dev.txt
#   (the requirements*.txt files are generated from uv.lock by `make lock`)
make hooks   # pre-commit: ruff, ruff-format, uv-lock, hygiene (developer machines only —
             # swarm agents have no egress; their in-container ruff gate is the equivalent)
(cd relay && npm install)
make relay                # terminal 1 — outbox relay
make agent ROLE=backend   # terminal 2 — one agent worker
make inject               # terminal 3
```

Point it at real work: run your own Forgejo, store a bot token in OpenBao
(`secret/data/swarm/forgejo`), label issues `swarm-ready` (+ `role:backend`
etc.), and schedule `make ingest`. See [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Repo layout

```
AGENTS.md identity.md user.md guardrails.md   # agent context (+ conventions/)
karpathy-guidelines.md # vendored coding guidelines (Appendix D; scripts/update_karpathy.sh)
proto/swarm/v1/        # Protobuf contracts (Task, AgentEvent) + buf config
agent/                 # Python 3.12 worker: claim -> aider -> outbox emit
agent/gen/             # checked-in generated protobuf code (make gen to refresh)
relay/                 # TypeScript outbox relay — the ONLY JetStream publisher
ingest/                # Forgejo issue sync (primary) + Telegram chat ingress
skills/                # sandboxed executable skills + seccomp'd runner
db/schema.sql          # tasks · outbox · lessons (pgvector, writable memory)
deploy/compose.yaml    # Tier A stack;  deploy/k8s/ = Tier B (k3s + Istio ambient)
deploy/ios-build/      # optional macOS iOS-build runner (Appendix A)
.forgejo/              # CI gates: ruff, pytest+cov, bandit, diff-size,
                       #   headroom-ai compression gate, license gate
scripts/               # bootstrap, schema registration, task injection, qualification
```

## Hard rules (enforced — see `guardrails.md`)

1. Agents have **no internet egress** and no `.ssh`/host mounts.
2. The **transactional outbox is the only publisher** into JetStream.
3. Task claiming is **atomic** (Postgres compare-and-set / `SKIP LOCKED`).
4. The bot pushes **only to `feature/*`**; `main`/`integration` are protected.
5. Auto-merge targets **`integration` only**, never `main`.
6. Skills run **inside a sandbox** (gVisor/Kata on Tier B; podman
   `--network=none --read-only --cap-drop=ALL` + seccomp on Tier A).
7. Secrets are **leased from OpenBao**, never baked into images or env files.
8. Test environments use **synthetic data only**.
9. **Three-zone license gate** on every PR (`conventions/licensing.md`):
   everything that ships — our code and every image's contents (Zone C) — is
   Apache/MIT/BSD/MPL/ISC/0BSD only, and that gate is **fail-closed**; nothing
   linked anywhere in the repo may be AGPL/SSPL/BUSL/Commons Clause (Zone B);
   network services we only call over a socket, unmodified in their own
   container (Forgejo, OpenObserve, Grafana …), may carry any OSI license (Zone A).

## Failure semantics

| Failure | Response |
|---|---|
| Two agents grab the same task | compare-and-set claim → exactly one owner |
| Skill throws repeatedly | `max-deliver=5` + backoff → `MAX_DELIVERIES` → `SWARM_DLQ` |
| Agent crashes mid-task | message un-acked → redelivered after ack-wait |
| Event published but state lost | impossible — state + event commit in one tx |
| Relay crashes mid-batch | `Nats-Msg-Id` + 2m dupe window dedupes at the server |
| Leaked secret | short-TTL OpenBao lease, auto-revoked |
| AGPL/SSPL dep sneaks in | license gate blocks the PR |

## Models (lineup by hardware tier — v4.1 T4.5)

| Hardware | Model / tier | Serving |
|---|---|---|
| 8–12 GB GPU | Ornith-1.0-9B Q4 (`9b-q4`) | `make models` |
| 24 GB | Ornith-1.0-35B Q4_K_M (`35b-q4` — MoE ~3B active: faster *and* better) | `make models-35b` / vLLM |
| 48–80 GB | Ornith-1.0-9B bf16 (`9b-bf16`) / 35B FP8 | `make models-bf16` / vLLM |
| 8×80 GB or API | Ornith-1.0-397B | vLLM multi-GPU, or hosted via the LiteLLM opt-in fallback |
| Fallback | gpt-oss:20b or qwen2.5-coder (Apache-2.0) | Ollama |
| Embeddings (any hardware, CPU) | fastembed `BAAI/bge-small-en-v1.5` (384-dim, ONNX — no torch); option `BAAI/bge-m3` (1024) via the same fastembed path | `make sync EXTRAS="embeddings"` |

KV-cache headroom matters at 256K context: the tier thresholds in
`agent/capacity.py` (`9b-q4` ≈8 GB, `35b-q4` ≈24 GB, `9b-bf16` ≈22 GB
free VRAM) include it — a node never advertises a tier it would spill on
(Ollama CPU spill is detected and demotes the node; spill is 5–30×
slower). vLLM needs the model's parsers: `--tool-call-parser qwen3_xml
--reasoning-parser qwen3`. Cutover rule: switch Ollama → vLLM past ~5
concurrent agents.

> **License note:** Ornith-1.0 is plain MIT and passes the gate. It is
> post-trained on Gemma 4 (non-OSI upstream terms); counsel has reviewed
> and cleared the MIT release for production use under the fail-closed
> policy. Pin the exact release — the 2026 model field is full of non-OSI
> "community"/"modified MIT" look-alikes the gate will (correctly) block.

## Every documented option is executable

The baseline is always on; each optional swap/enhancement from the design
docs ships runnable:

| Option (docs) | Baseline | Run the option |
|---|---|---|
| vLLM inference (>5 agents) | Ollama | `make vllm`, then `AIDER_MODEL=openai/Ornith-1.0-9B OPENAI_API_BASE=http://localhost:8000/v1` (Tier B: `deploy/k8s/vllm.yaml`) |
| Ornith-1.0-35B (24 GB+) | 9B | `make models-35b` + `AIDER_MODEL=ollama/maxwell1500/ornith-35b:Q4_K_M` |
| Fallback models | Ornith-1.0 | `AIDER_MODEL=ollama/gpt-oss:20b` or `ollama/qwen2.5-coder:7b` |
| nano-claude-code engine | aider | `AGENT_ENGINE=nano-claude-code` (custom: `AGENT_ENGINE_CMD="tool {message}"`) |
| Qdrant vector store (>5–10M vectors) | pgvector | `make qdrant` + `VECTOR_BACKEND=qdrant` (`pip install -r requirements-vector.txt`) |
| Chroma local-tier retrieval (retained v3.0 option) | pgvector | `VECTOR_BACKEND=chroma` |
| bge-m3 embeddings (quality > speed) | fastembed bge-small-en-v1.5 (384) | apply `db/migrations/001_bge_m3_embeddings.sql`, set `EMBEDDING_MODEL=BAAI/bge-m3 EMBEDDING_DIM=1024` (served by fastembed from the ONNX export), then **mandatory** `scripts/seed_knowledge.py --reembed` |
| sentence-transformers embeddings (torch) | fastembed (ONNX, no torch) | `make sync EXTRAS="embeddings-torch"` + `EMBEDDING_BACKEND=sentence-transformers` (default model all-MiniLM-L6-v2, 384), then **mandatory** `scripts/seed_knowledge.py --reembed` |
| LangGraph orchestration (durable graphs, HIL) | bespoke loop | `make graph` (`HIL=1` pauses before work; `requirements-orchestration.txt`) |
| CrewAI declarative crews | bespoke roles | `make crew TITLE="..."` |
| CDC outbox relay (higher throughput) | 250ms poll | `make relay-cdc` (publication ships in `db/schema.sql`) |
| Canonical observability: OpenSearch logs · Jaeger traces · Prometheus + **Perses** SLA dashboard · Langfuse LLM traces | log lines | `make observability` (agents export OTLP to Jaeger and serve `/metrics`; Perses on :8090, Jaeger on :16686) + `LANGFUSE_*`/`OPENSEARCH_URL` envs (`agent/tracing.py` fans out) |
| Grafana SLA dashboard (AGPL-3.0, Zone A) | Perses | import `deploy/observability/grafana-sla.json` into your Grafana; `make perses-migrate` converts it for Perses |
| OpenObserve single-binary swap | OpenSearch+Langfuse | `make openobserve` + `OTEL_EXPORTER_OTLP_ENDPOINT` |
| OTel tracing (Laminar/OpenLLMetry-style) | Langfuse | `OTEL_EXPORTER_OTLP_ENDPOINT` towards any collector |
| Kata/Firecracker sandbox (dedicated kernel) | gVisor | `runtimeClassName: kata` (`deploy/k8s/runtimeclass-gvisor.yaml` ships both) |
| Slack / Discord ingress | Telegram | `make chat` with `CHAT_SOURCES=telegram,slack,discord` + channel ids |
| More agent roles | 4 roles | all four run under `make swarm` / `deploy/k8s/agent-roles.yaml`; add a fifth per Appendix B |
| Forgejo self-hosted git | external git | `make forgejo`, then `scripts/forgejo_setup.sh` + `scripts/forgejo_automerge.sh <pr>` |
| iOS build container | Linux CI only | `deploy/ios-build/` (macOS runner workflow) |
| Confluent ccompat SerDes | direct Protobuf | `agent/ccompat.py` (`requirements-ccompat.txt`) |
| DLQ → human triage (Stage 4) | — | `make dlq` (opens `needs-human` Forgejo issues) |
| SLA tracking (≥70% / <10 min) | — | `make sla` |
| Knowledge-pack seeding (Appendix C) | — | `make seed` after dropping docs in `knowledge/` |
| Karpathy coding guidelines (Appendix D) | — | vendored as `karpathy-guidelines.md`, imported by `AGENTS.md` + aider; refresh with `scripts/update_karpathy.sh` |

## v4.1.1 "Lean & Precise" — smaller footprint, sharper code generation

Nothing from v4.0/v4.1 was removed; every option above still runs. What
changed (task-by-task evidence: [docs/loop/STATE.md](docs/loop/STATE.md),
decisions: [docs/loop/DECISIONS.md](docs/loop/DECISIONS.md), ledger:
[docs/loop/LEDGER.md](docs/loop/LEDGER.md), sizes:
[docs/footprint-baseline.md](docs/footprint-baseline.md)):

| Area | What runs |
|---|---|
| Toolchain | `pyproject.toml` + `uv.lock` are the source of truth (`make lock`, `make sync`); `requirements*.txt` are generated for the pip fallback; CI installs from the lock with a cache; `make ci-local` runs the same gates; `make hooks` installs pre-commit |
| Images | `.dockerignore`, multi-stage agent/ingest/relay images (uv/npm builders, slim and distroless runtimes); headroom-ai lives only in the ingest image (it cannot co-resolve with aider) |
| Lite path | `make up-lite` (nats, postgres+pgvector, valkey, openbao) + `make ollama`; `make clean`, `make disk-usage`; capped sandbox tmpfs and orphan-workdir reaping |
| Quality gate | two stages — a fast lint of the Python files the task changed, then the full checks + the task's verify command (`QUALITY_FAST_CHECK`); stage timings in `make sla` |
| Retrieval | hybrid lesson search (pgvector + Postgres full-text, RRF); fastembed default embedder (no torch); signature index for Python/TS/JS/Go/Rust/Java, incremental (`make index-repo`) |
| Storage | morning routine prunes published outbox rows (`OUTBOX_RETENTION_DAYS`) and runs `VACUUM (ANALYZE)` |
| Licensing | three-zone gate: Zone C (ships) fail-closed permissive, Zone B (linked) denylist, Zone A (network services) any OSI licence — `make gate` |
| Observability | Jaeger + Prometheus + Perses beside OpenSearch and Langfuse; agents serve the SLA gauges on `/metrics` |
| Evaluation | ten-task golden pack, two per role (`python scripts/shadow_eval.py --dry-run`), per-role deltas |

Developer and operator targets added in v4.1.1:

| Target | Does |
|---|---|
| `make lock` | `uv lock` + regenerate every `requirements*.txt` (idempotent) |
| `make sync` | `uv sync --frozen` — `GROUPS="agent dev"`, `EXTRAS="embeddings"` (never agent + ingest together) |
| `make hooks` | install the pre-commit hooks (ruff, ruff-format, uv-lock, hygiene) |
| `make ci-local` | every CI gate with the same commands |
| `make up-lite` / `make down-lite` / `make ollama` | the lite spine and on-demand Ollama |
| `make clean` | prune stopped containers, dangling images and volumes (keeps the swarm's data volumes) |
| `make disk-usage` | container disk usage |
| `make footprint` | wheel sizes per requirement set (+ image sizes when a runtime is up) |
| `make gate` | license gate zones C and B, and the Zone A services table |
| `make perses-migrate` | convert the Grafana dashboard with `percli` |
| `make tools` / `make ingest-container` | the isolated `headroom` CLI / one issue sync inside the ingest image |
| `make torch-cpu-lock` | re-lock the torch extras to CPU-only wheels (needs download.pytorch.org) |
| `make verify-integration` | every check that needs a container runtime, GPU or the running spine — run it on the runtime host |

Upgrading from v4.1: `make migrate` (migrations 006–009), re-run
`make bootstrap`, then `python scripts/seed_knowledge.py --reembed` (the
default embedder changed; a v4.1 `.env` that pins
`EMBEDDING_MODEL=all-MiniLM-L6-v2` keeps the same vectors and needs no
re-embed) and `make index-repo REPO=<path>` to rebuild the signature index.

## v4.1 "Elevation" — best code as output

Every item of the v4.1 development plan is implemented (26/26 coverage
matrix). The north-star metrics: first-pass green rate (`make sla`),
reviewer catch rate (`make morning`), tokens per merged PR (Headroom),
and the protected SLAs (dashboard: `deploy/observability/grafana-sla.json`).

| Workstream | What runs |
|---|---|
| Headroom compression (T1.1–T1.9) | CI gate on fixtures (`.forgejo/scripts/headroom_gate.py`), proxy sidecar (`make headroom`, offline assets via `scripts/seed_headroom_assets.sh`), agent wiring (`HEADROOM_ENABLED=1`), `headroom learn` → PR (`scripts/morning_headroom_learn.py`), output shaper + measured savings in `make sla`, cross-process Valkey hand-off compressed in flight by the proxy (`agent/shared_context.py`), ingest compression with SeaweedFS originals (`ingest/compress_util.py`), compression attrs in traces |
| Code quality (T2.1–T2.6) | green-build gate + grounded repair loop in `agent/sandbox.py` (`MAX_REPAIR_CYCLES`, red never pushes), repro-test-first policy (`conventions/repro-tests.md`), independent verifier (`agent/verifier.py`, Architect/Security/QA lenses), bounded repo RAG (`make index-repo`, `CONTEXT_PACK=1`), speculative patches (`make speculative TASK=<id>`), repair-cap → DLQ |
| Planner–executor–verifier (T3.1–T3.3) | verification-aware DAG in `make graph` (subtasks carry `parent_task_id`+`verify_cmd`; replans on verify-fail), single-agent-default policy (`conventions/orchestration.md`), Postgres checkpoints (`GRAPH_CHECKPOINT=postgres`) |
| Capacity + routing (T4.1–T4.5) | node capacity probe (`agent/capacity.py`, `WORKER_TIERS=auto`), tier subjects `swarm.tasks.<role>.<tier>` (backward-compatible default), LiteLLM gateway with structural local-primary (`make litellm`, `ROUTE_API_FALLBACK` opt-in), RouteLLM proposal (`ROUTER_ENABLED=1`), hardware lineup above |
| Hardening + observability (T5.1–T5.3) | backoff ladder 1s→10m + `UNIQUE(msg_id)` + DLQ replay (`make dlq-replay SEQ=<n>`), OTel GenAI spans + Grafana SLA dashboard, expensive-failure circuit breaker (`reason=snowball`) |
| Evaluation + ops (T6.1–T6.4) | shadow-eval harness gates every default flip (`make shadow-eval CANDIDATE=KEY=VAL`), morning verifier audit (`make morning`), skills as MIT modules (`skills/*/skill.yaml` + LICENSE), context-management policy (`conventions/context.md`) |

Honest caveats (from the plan): Headroom saves 15–20% on coding-agent
workloads — the 60–95% headline is for JSON tool outputs; judge on the
`make sla` measured numbers. The proxy adds a hop — watch p95 latency and
bypass per-role with `HEADROOM_ENABLED=0`. Existing deployments: run
`make migrate` (migrations 002–005) and re-run `make bootstrap`
(tier consumers + backoff ladder; the stream subject widens to
`swarm.tasks.>`).

## Tier B (production: k3s + Istio ambient)

Same repo, same contracts, same code. `deploy/k8s/install.sh` brings up
k3s (no Traefik) + Istio ambient (mind the `global.platform=k3s` CNI
overrides), the platform services via Helm (JetStream `Replicas: 3`),
default-deny egress, gVisor `RuntimeClass` for skills, the OpenBao
injector for secrets, and vLLM for inference.

## Qualification gates (pass before unattended overnight runs)

1. **Spine** — 100 synthetic tasks (`scripts/inject_task.py --count 100`),
   then `scripts/qualify_stage1.py`: zero dual-write losses, zero
   duplicate claims; one clean `feature/*` → `integration` cycle.
2. **Boundary** — `skills/run_skill.sh hello --prove-isolation` shows no
   network/host FS from the sandbox; a deliberate AGPL dep is blocked in CI.
3. **Scale** — p95 task-pickup latency flat from 4 → 16 agents on vLLM.
4. **Autonomy** — full traceability, Valkey budgets enforced, DLQ→human
   triage path works. SLAs (tuning signals): ≥70% of tasks done in 24h,
   average message latency < 10 min.

**Pause autonomy if:** a task DLQs for a non-transient reason, an agent
busts its budget more than once a night, or an `integration` merge is one
a human would reject. Fix the rule/guardrail/skill — not the code.

## Notes on deviations from the runbook

- `postgres:16` → **`pgvector/pgvector:pg16`** (the schema needs
  `CREATE EXTENSION vector`).
- The relay is plain Node 22 + TypeScript (`nats` + `pg`), matching the
  runbook's relay code; adopt NestJS if you grow service structure.
- Generated protobuf code is **checked in** (`agent/gen/`) so a fresh
  clone runs without `buf`; regenerate with `make gen` (or `buf generate`).
- Tier A JetStream defaults to `REPLICAS=1` (single node); use
  `REPLICAS=3` on Tier B.
