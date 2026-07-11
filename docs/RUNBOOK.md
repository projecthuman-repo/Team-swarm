# Operations runbook

Condensed from the v4.0 build runbook. Follow top to bottom; every section
ends with its **Verify** step. Tier A (Podman) is the dev path; Tier B
(k3s + Istio ambient) is production. Same repo, contracts, and code.

## Version pins

| Tool | Pin |
|---|---|
| NATS Server | 2.12.x |
| Apicurio Registry | 3.3.x |
| PostgreSQL | 16 + pgvector |
| OpenBao | 2.5.0 |
| Valkey | 9.0 |
| SeaweedFS | 4.3x |
| k3s / Istio | 1.36.x / 1.30.x (ambient GA since 1.24) |
| Node / Python | 22 / 3.12 |

## 1. Contracts

`proto/swarm/v1/*.proto` are the wire contracts. Regenerate code with
`make gen` (grpcio-tools) or `buf generate`. CI registers them with
Apicurio (`scripts/register_schemas.sh`) where a BACKWARD compatibility
rule rejects breaking changes.

*Python SerDes caveat:* Apicurio has no first-class Python SerDes — agents
use the generated Protobuf classes directly (this repo's approach). For
Confluent wire-compatibility, point confluent-kafka's Protobuf SerDes at
Apicurio's ccompat endpoint (`/apis/ccompat/v7`,
`apicurio.registry.headers.enabled=false`).

**Verify:** `make gen` is a no-op diff; schema POST returns 200 and the
artifacts appear in the Apicurio UI (http://localhost:8081).

## 2. Database

`db/schema.sql` — applied automatically on first `make up` (initdb mount)
and idempotently by `make bootstrap`.

**Verify:** `\dt` lists `tasks`, `outbox`, `lessons`; `\d lessons` shows
`vector(384)` and the hnsw index.

## 3. JetStream

`scripts/bootstrap_jetstream.sh` creates:

- `SWARM_TASKS` — work-queue retention, `swarm.tasks.*`, 2m dupe window;
- one durable pull consumer per role (`--max-deliver=5 --backoff=linear
  --ack-wait=10m --max-pending=8`);
- `SWARM_EVENTS` — limits retention, 168h;
- `SWARM_DLQ` — captures the `MAX_DELIVERIES` advisory (JetStream has no
  built-in DLQ).

Semantics: work-queue + durable pull = at-least-once with load-balancing;
"exactly-once" is really at-least-once **plus** the outbox dedupe
(`Nats-Msg-Id`) and the compare-and-set claim. Never assume the bus alone.

**Verify:** `nats stream ls` shows all three; `nats consumer ls
SWARM_TASKS` shows one consumer per role.

## 4. Relay

`relay/relay.ts` — the only publisher (Hard Rule 2). Safe to run multiple
instances (`FOR UPDATE SKIP LOCKED`).

**Verify:** insert an outbox row manually; within ~250 ms `published_at`
is set and `nats stream view SWARM_TASKS` shows the message.

## 5. Agents

`agent/agent_worker.py`, one process per role via `AGENT_ROLE`. Add a role
(Appendix B): use subject `swarm.tasks.<role>`, add a durable consumer,
deploy a worker with `AGENT_ROLE=<role>` — bus, outbox, relay unchanged.

**Verify:** start two backend workers, inject one task: exactly one claims
it. Kill a worker mid-task: after ack-wait (10m) it redelivers to the other.

## 6. Skills

`skills/run_skill.sh <name>` — Tier A podman sandbox (no network,
read-only, no caps, seccomp). Tier B: `deploy/k8s/skill-runner.yaml`
(gVisor; swap to `kata` for a dedicated kernel).

**Verify:** `skills/run_skill.sh hello --prove-isolation` reports the
sandbox blocked network and writes.

## 7. Secrets (OpenBao)

Tier A: dev server + `make bootstrap` seeds placeholder paths — replace:

```bash
curl -X POST http://localhost:8200/v1/secret/data/swarm/forgejo \
  -H "X-Vault-Token: dev-root" -d '{"data":{"token":"<bot token>"}}'
```

Tier B: enable Kubernetes auth and the injector:

```bash
bao policy write swarm-agent - <<'EOF'
path "secret/data/swarm/forgejo" { capabilities = ["read"] }
EOF
bao auth enable kubernetes
bao write auth/kubernetes/role/swarm-agent \
  bound_service_account_names=swarm-agent \
  bound_service_account_namespaces=swarm \
  policies=swarm-agent ttl=15m
```

**Verify:** token TTL ≤ 15m; revoke the lease and the agent's next Forgejo
call 401s.

## 8. Forgejo wiring

1. Protect `main` and `integration`; the bot account may push only to
   `feature/*`.
2. Run the Actions runner as a non-privileged container on a separate
   host/node from the Forgejo server (label `docker`).
3. The bot token comes from OpenBao — never stored in the repo.
4. Auto-merge `feature/*` → `integration` only when all checks pass:

```bash
curl -X POST "$FORGEJO/api/v1/repos/$OWNER/$REPO/pulls/$PR/merge" \
  -H "Authorization: token $FORGEJO_TOKEN" \
  -d '{"Do":"merge","merge_when_checks_succeed":true}'
```

**Verify:** a PR to `integration` merges only after green checks; a bot
push to `main` is rejected; auto-merge to `main` is never configured.

## 9. Task ingestion

- **Primary:** `ingest/forgejo_issue_sync.py` on a schedule — `swarm-ready`
  labelled issues become tasks (role from `role:<name>`, dedupe on
  `ext_id`, idempotent re-runs).
- **Chat:** `ingest/chat_ingress.py` — Telegram baseline (Slack/Discord
  follow the same pattern): message → triage task through the outbox.

**Verify:** label a synthetic issue `swarm-ready` + `role:backend` → a
task row appears within one cron tick, the relay publishes to
`swarm.tasks.backend`, the backend agent claims it; re-running the sync
creates no duplicate.

## 10. CI gates

All required, any non-zero exit blocks auto-merge: `ruff`, `pytest --cov`
(floor), `bandit`, diff-size (`.forgejo/scripts/diff_size_gate.sh`),
headroom CLI, and the fail-closed license gate
(`.forgejo/scripts/license_gate.py`, needs [syft](https://github.com/anchore/syft)).

**Verify:** add an AGPL dep on a test branch → gate exits 1, merge blocked.

## 11. Observability

Emit every agent action/prompt/diff/token-count/check-result to Langfuse
(traces) and OpenSearch (logs — `make observability`). Optional
single-binary swap: OpenObserve.

**Verify:** for any merged diff you can reconstruct the originating
prompt, token spend, and check results from the trace.

## 12. Optional upgrade triggers (all runnable — see README options matrix)

| Layer | Baseline | Swap to | When | How |
|---|---|---|---|---|
| Inference | Ollama | vLLM | >5 concurrent agents | `make vllm` / `deploy/k8s/vllm.yaml` |
| Vector store | pgvector | Qdrant | >5–10M vectors / heavy filtered search | `make qdrant` + `VECTOR_BACKEND=qdrant` |
| Local retrieval | pgvector | Chroma (retained v3.0 option) | laptop-only runs | `VECTOR_BACKEND=chroma` |
| Orchestration | bespoke loop | LangGraph | durable resumable graphs / HIL | `make graph` (`HIL=1`) |
| Roles | bespoke | CrewAI | declarative crews (weaker observability) | `make crew` |
| Sandbox | gVisor | Kata/Firecracker | dedicated-kernel threat model | `runtimeClassName: kata` |
| Observability | OpenSearch+Langfuse | OpenObserve | storage cost / tool sprawl | `make openobserve` + OTLP env |
| Tracing | Langfuse | OTel (Laminar/OpenLLMetry-style) | vendor-neutral instrumentation | `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Embeddings | all-MiniLM-L6-v2 | bge-m3 | retrieval quality > speed | migration 001 + `EMBEDDING_MODEL` |
| Relay | 250ms poll | logical-replication CDC | outbox throughput | `make relay-cdc` |
| Engine | aider | nano-claude-code | lightweight runtime | `AGENT_ENGINE=nano-claude-code` |
| Chat ingress | Telegram | +Slack/Discord | team-channel task initiation | `CHAT_SOURCES=telegram,slack,discord` |

Stage-4 operational tooling: `make dlq` (DLQ → `needs-human` Forgejo
issues), `make sla` (completion rate, publish→claim latency, p95 pickup),
`make seed` (Appendix C knowledge packs into vector memory).

## Agent coding guidelines (Appendix D)

`karpathy-guidelines.md` is vendored from `multica-ai/andrej-karpathy-skills`
(MIT — passes the §19 gate) and imported into every agent's context via
`AGENTS.md` and aider's `read:` list, so it applies to aider and
nano-claude-code alike. Its four principles map onto controls the swarm
already enforces:

| Principle | Demands | Reinforces |
|---|---|---|
| Think Before Coding | state assumptions; ask when ambiguous | "teacher not doer"; fewer bad diffs |
| Simplicity First | minimum code, no speculative abstractions | diff-size gate (§10) |
| Surgical Changes | touch only what the task requires; flag dead code, don't remove it | diff-size gate; guards against silent-drop regressions |
| Goal-Driven Execution | define success criteria; loop until verified | CI gates + qualification SLAs |

Agents have no egress, so the file is vendored — refresh it from a host
with `scripts/update_karpathy.sh`, never at runtime.

**Verify:** an agent given an ambiguous ticket opens a clarifying question
instead of guessing; its PR touches only files the task names and contains
no drive-by refactors of adjacent code.

## Model licensing status

Ornith-1.0 is plain MIT and passes the gate. Its Gemma 4 upstream terms
(non-OSI) have been **reviewed and cleared by counsel** for production use
under the fail-closed policy. Keep the exact release pinned; the gate
still blocks non-OSI look-alike releases.

## Known gotchas

- **k3s + Istio-CNI paths:** some k3s versions (>1.31.6, esp. k3d) broke
  Istio-CNI defaults — keep `global.platform=k3s` +
  `cniConfDir=/var/lib/rancher/k3s/agent/etc/cni/net.d`, pin versions.
- **Forgejo is GPLv3:** fine as an isolated network service; never link it
  into a shipped artifact.
- **Benchmarks are directional:** re-measure Ollama vs vLLM on your own
  hardware before the Tier-B cutover.
