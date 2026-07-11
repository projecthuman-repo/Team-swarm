# Team-swarm — Overnight Agent Swarm v4.0

A self-hosted, licence-clean multi-agent coding swarm: autonomous agents
(backend / frontend / review / triage) pick up tasks overnight, write code
with [aider](https://github.com/Aider-AI/aider) + **Ornith-1.0** on local
inference, open PRs on your own Forgejo, and merge to `integration` only
when every CI gate is green. No cloud calls, no agent egress, everything
permissively licensed (Apache/MIT/BSD/MPL — enforced by a fail-closed gate).

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
                     OpenSearch + Langfuse (logs · traces · token spend)
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

Run components on the host instead of in containers (nicer for hacking):

```bash
cp .env.example .env
pip install -r requirements.txt -r requirements-dev.txt
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
                       #   headroom, fail-closed license gate
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
9. **Fail-closed license gate** on every PR: Apache/MIT/BSD/MPL only.

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

## Models

| Hardware | Model | Serving |
|---|---|---|
| 6–12 GB GPU | Ornith-1.0-9B (Dense, MIT) | `ollama pull maxwell1500/ornith-9b:Q4_K_M` |
| 24 GB+ | Ornith-1.0-35B (MoE, ~3B active — faster *and* better) | Ollama Q4 / vLLM (`deploy/k8s/vllm.yaml`) |
| Fallback | gpt-oss:20b or qwen2.5-coder (Apache-2.0) | Ollama |

vLLM needs the model's parsers: `--tool-call-parser qwen3_xml
--reasoning-parser qwen3`. Cutover rule: switch Ollama → vLLM past ~5
concurrent agents.

> **License note:** Ornith-1.0 is plain MIT and passes the gate, but it is
> post-trained on Gemma 4, whose upstream terms are not OSI — confirm with
> counsel before production use.

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
