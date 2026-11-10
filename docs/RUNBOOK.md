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
| uv | 0.11 (lock written by 0.11.32) |
| Jaeger / Prometheus / Perses | 2.22.0 / v3.15.1 / v0.55.1 |
| fastembed / prometheus-client | 0.9.x / 0.26.x |

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

**Migrations (v4.1.1).** Existing databases run `make migrate`; fresh
installs get everything from `db/schema.sql`. All are idempotent, and the
target stops at the first error. It applies only the files directly in
`db/migrations/` (002–009). The bge-m3 switch,
`db/migrations/optional/001_bge_m3_embeddings.sql`, resizes
`lessons.embedding` to 1024 dimensions and clears every embedding, so it
is applied by hand and never by `make migrate`.

| migration | adds |
|---|---|
| `006_gate_timing.sql` | `tasks.gate_stage1_ms`, `tasks.gate_stage2_ms` — first-pass timings of the two-stage gate |
| `007_lessons_fts.sql` | `lessons.fts` (generated tsvector) + GIN index + `match_lessons_hybrid()` (dense + full-text, RRF) |
| `008_lessons_source.sql` | `lessons.source`, `lessons.content_sha` + a unique index on `source` for `tag='codex-index'` (incremental signature index) |
| `009_task_tokens.sql` | `tasks.tokens_spent` (source of the `swarm_tokens_per_merged_pr` gauge) |

**Verify:** `\df match_lessons_hybrid` lists the function; `make
index-repo REPO=.` twice reports every record unchanged the second time.

**Embeddings (L2.3).** The default embedder is fastembed (ONNX Runtime, no
torch — extra `embeddings`) with `BAAI/bge-small-en-v1.5`, 384-dim, matching
`vector(384)`. Options: `EMBEDDING_MODEL=BAAI/bge-m3 EMBEDDING_DIM=1024`
(after migration 001; same fastembed path) or
`EMBEDDING_BACKEND=sentence-transformers` (extra `embeddings-torch`; default
model all-MiniLM-L6-v2). Any switch of backend or model changes the vector
space — `python scripts/seed_knowledge.py --reembed` is **mandatory** after
it, or dense search compares incompatible vectors. The upgrade from the
v4.1 default (all-MiniLM-L6-v2) to bge-small is such a switch.

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
the Headroom compression gate (headroom-ai fixtures), and the fail-closed license gate
(`.forgejo/scripts/license_gate.py`, needs [syft](https://github.com/anchore/syft)).

**Locked CI (v4.1.1).** The `gates` job installs from the lockfile
(`uv sync --frozen --group agent --group dev`, uv cache keyed on
`uv.lock`) and fails if the lock is stale (`uv lock --check`), if the
`embeddings` extra pulls torch, if `requirements*.txt` differ from a fresh
`make lock`, or if `agent/gen/` differs from a fresh protoc run. Every gate
runs with `uv run --no-sync`. The Headroom gate runs in its own venv
(headroom-ai cannot co-resolve with aider). `make ci-local` runs the same
commands on a developer machine; `make hooks` adds the pre-commit subset.
Required checks on the protected branches: `gates` and `proto-compat`.

**License zones.** The gate runs twice (`conventions/licensing.md`).
`--zone C` — what is distributed, permissive allowlist, fail-closed:
- *Python.* For every package in `uv.lock` the gate reads the publisher's
  metadata on pypi.org for the locked version (cached in
  `.cache/license-gate/`): the SPDX `license_expression`, else an SPDX id in
  `license`, else the trove classifiers. The closures of the agent, ingest
  and headroom images (`uv export`, by name and version) are judged by the
  allowlist; lock entries in no image by the Zone B denylist.
  `--python-only` runs just this, without syft — `make ci-local` does.
- *Everything else.* syft scans `agent ingest relay skills scripts` and
  `relay/package-lock.json`.
- A package with no declared license blocks unless it has a reviewed row in
  "License overrides". A row cannot launder: publisher text or classifiers
  naming a copyleft or proprietary family block whatever the row says. The
  only way past that is a version-pinned row in "Bundled-runtime
  acknowledgements" (numpy, scipy: the GCC runtime libraries in the wheel).
- A failed metadata fetch, a failed `uv export`, a missing path, a failed
  scan or a scan that finds nothing is exit 2. The gate needs pypi.org or a
  warm cache.

`--zone B` — the whole repo and every lock entry: AGPL/SSPL/BUSL/Commons
Clause denylist. `--zone A` prints the network-services table (Forgejo
GPL-3.0-or-later, OpenObserve and Grafana AGPL-3.0, …) and exits 0.
`make gate` runs all three.

**Diff size.** Measured against `integration` when it exists, else `main`;
enforced on the auto-merge path (`integration`), reported for PRs into
`main`; `uv.lock`, the requirements files `make lock` writes and the protoc
outputs under `agent/gen/` are excluded by exact name because CI proves
they are generated — any other file beside them is counted.

**Verify:** add an AGPL dep to the `agent` group on a test branch (`uv add`,
so it is in `uv.lock`) → zone C and zone B both exit 1, merge blocked; add
a GPL-3.0 dep → zone C exits 1, zone B passes; add the same GPL-3.0 dep to
an opt-in extra instead → both pass (it ships in no image); run
`--zone C --python-only` with pypi.org blocked and an empty cache → exit 2,
never 0.

## 11. Observability

Emit every agent action/prompt/diff/token-count/check-result to the
canonical stack (Lean & Precise L3.2), all in the `observability` compose
profile (`make observability`):

| signal | backend | where |
|---|---|---|
| logs | OpenSearch | `OPENSEARCH_URL`, index `swarm-logs` |
| traces | **Jaeger** (`jaegertracing/jaeger:2.22.0`) | agents export OTLP/HTTP to `http://jaeger:4318` (compose default of `OTEL_EXPORTER_OTLP_ENDPOINT`); one trace per task (trace id = task id), one span per event; UI/API on :16686 |
| metrics | **Prometheus** (`prom/prometheus:v3.15.1`) | scrapes every agent's `/metrics` (health port 8080 — the SLA gauges of `agent/sla_metrics.py`, refreshed every 60 s) and the relay (`:9464`) — `deploy/observability/prometheus.yml` |
| dashboards | **Perses** (`persesdev/perses:v0.55.1`) | `deploy/observability/perses-sla.json` provisioned into project `swarm` on :8090 (70 %/24 h and 10-minute SLA thresholds) |
| LLM traces | Langfuse | `LANGFUSE_HOST` + keys |

Options: OpenObserve (`make openobserve`, single binary; point
`OTEL_EXPORTER_OTLP_ENDPOINT` at it) and Grafana (import
`deploy/observability/grafana-sla.json`; AGPL-3.0, Zone A).
`make perses-migrate` converts the Grafana dashboard with `percli` against
the compose Perses when refining the Perses one. The gauges:
`swarm_tasks_done_24h_ratio`, `swarm_pickup_latency_p95_seconds`,
`swarm_first_pass_green_ratio`, `swarm_tokens_per_merged_pr`,
`swarm_headroom_savings_ratio`, `swarm_dlq_depth`, `swarm_snowball_total`,
`swarm_gate_stage1_ms_median`, `swarm_outbox_rows` — `make sla` prints the
same numbers from the same queries.

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
| Embeddings | fastembed bge-small-en-v1.5 (384) | bge-m3 via fastembed (1024); sentence-transformers (torch) | retrieval quality > speed | migration 001 + `EMBEDDING_MODEL`/`EMBEDDING_DIM` (or `EMBEDDING_BACKEND`), then `seed_knowledge.py --reembed` |
| Relay | 250ms poll | logical-replication CDC | outbox throughput | `make relay-cdc` |
| Engine | aider | nano-claude-code | lightweight runtime | `AGENT_ENGINE=nano-claude-code` |
| Chat ingress | Telegram | +Slack/Discord | team-channel task initiation | `CHAT_SOURCES=telegram,slack,discord` |

Stage-4 operational tooling: `make dlq` (DLQ → `needs-human` Forgejo
issues), `make sla` (completion rate, publish→claim latency, p95 pickup),
`make seed` (Appendix C knowledge packs into vector memory).

## v4.1 operations

- **Migrations:** existing DBs run `make migrate` (002 lessons.tag,
  003 task quality, 004 node_caps, 005 outbox UNIQUE); fresh installs get
  everything from `db/schema.sql`. Re-run `make bootstrap` after
  upgrading — the task stream subject widens to `swarm.tasks.>` (edit the
  stream with `nats stream edit SWARM_TASKS --subjects='swarm.tasks.>'`
  on live deployments) and consumers gain the 1s→10m backoff ladder +
  tier consumers.
- **Headroom chain:** `scripts/seed_headroom_assets.sh` (egress host) →
  `make headroom` → per-role `HEADROOM_ENABLED=1`. Budgets meter what the
  engine actually sends (post-compression); the pre/post pair in traces
  is observability, not billing (T1.9). Verify no-egress: the proxy
  compresses while attached only to swarm-internal.
- **Fallback drill (T4.3):** stop Ollama; with `ROUTE_API_FALLBACK=1` +
  a stub `FALLBACK_API_BASE`, requests land on the stub; with the flag
  off they fail closed to JetStream retry and never egress.
- **Duplicate-delivery drill (T5.1):** force a redelivery (kill a worker
  mid-task); the CAS claim + `UNIQUE(msg_id)` mean no double-claim and no
  duplicate outbox row. Replay a DLQ'd task: `make dlq-replay SEQ=<n>`.
- **Adoption rule:** any default flip (model, scaffold, flag) must first
  beat the current default on `make shadow-eval` — leaderboards don't
  transfer.
- **Morning Operator Routine:** `make morning` = DLQ triage → SLA report
  (green rate + measured Headroom savings) → verifier audit of last
  night's merges → `headroom learn` dry-run (corrections ship as a
  feature/* PR, never a direct context write).

## Footprint (v4.1.1)

`make footprint` prints compressed wheel sizes per requirement set and,
with a container runtime, image sizes, `system df` and the SLA latency
lines; the per-phase tables are in `docs/footprint-baseline.md`.

- **Targets:** agent image ≤ 700 MB, ingest ≤ 300 MB, relay ≤ 160 MB
  (`scripts/image_sizes.sh`, run by `make verify-integration`).
- **Keeping it small:** the default embedder is fastembed (the
  `embeddings` set is ~60 MB of wheels; the torch route is opt-in via
  `embeddings-torch`, CPU-only wheels with `make torch-cpu-lock`);
  images are multi-stage with a `.dockerignore`; the relay ships as an
  esbuild bundle on distroless.
- **Lite path:** `make up-lite` runs four services instead of seven;
  `make ollama` adds inference on demand.
- **Disk hygiene:** `make disk-usage`, `make clean` (never removes the
  swarm's data volumes), agent `/tmp` capped by `SANDBOX_TMPFS_SIZE`,
  orphaned task workdirs reaped on worker boot, and the morning routine
  prunes published outbox rows (`OUTBOX_RETENTION_DAYS`) and vacuums.
- **Runtime-host checks:** `make verify-integration` runs everything a dev
  session cannot (image builds and sizes, syft scans, lite and
  observability smokes, real-Postgres tests, the embedder shadow-eval) and
  fails if any step fails.

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
