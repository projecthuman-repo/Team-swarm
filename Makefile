# Overnight Agent Swarm v4.0 — Tier A developer entry points.
# `make quickstart` = up + bootstrap.

RUNTIME := $(shell command -v podman || command -v docker)
COMPOSE := $(RUNTIME) compose -f deploy/compose.yaml
COMPOSE_LITE := $(RUNTIME) compose -f deploy/compose.lite.yaml
# HOSTPORTS=1 (review R5): also attach the port-publishing spine services to
# the non-internal `swarm-host` network — for runtimes that do not forward
# ports of internal-only containers (rootful Podman, Docker). Agents and the
# relay never join it. scripts/lite_smoke_modes.sh tells you whether this
# host needs it. Published ports bind to SWARM_BIND_ADDR (default 127.0.0.1).
HOSTPORTS ?= 0
ifeq ($(HOSTPORTS),1)
COMPOSE += -f deploy/compose.hostports.yaml
COMPOSE_LITE += -f deploy/compose.hostports.lite.yaml
HOSTPORTS_NET := ./scripts/hostports_network.sh
else
HOSTPORTS_NET := @true
endif

.PHONY: quickstart up down bootstrap swarm forgejo chat vllm qdrant \
        observability openobserve headroom litellm strix codex-engine \
        relay relay-cdc agent graph crew verify-diff codex-review \
        codex-audit agents-md-check escalation ingest inject qualify dlq dlq-replay \
        sla seed index-repo shadow-eval shadow-eval-engines strix-findings strix-scan \
        doomloop morning speculative test lint gate gen \
        models models-35b models-bf16 migrate logs footprint \
        verify-integration tools ingest-container lock sync torch-cpu-lock \
        ci-local hooks up-lite down-lite ollama clean disk-usage perses-migrate

quickstart: up bootstrap ## start infra and bootstrap everything

up: ## start the Tier A spine (nats, postgres, apicurio, valkey, seaweedfs, openbao, ollama); HOSTPORTS=1 for host-reachable ports
	$(HOSTPORTS_NET)
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

up-lite: ## lite spine: nats, postgres+pgvector, valkey, openbao (no apicurio/seaweedfs/ollama); HOSTPORTS=1 for host-reachable ports
	$(HOSTPORTS_NET)
	$(COMPOSE_LITE) up -d

down-lite: ## stop the lite spine (and the add-ons started on it: ollama, forgejo); keeps volumes
	$(COMPOSE_LITE) down --remove-orphans

ollama: ## start only the ollama service of the full compose file (lite path)
	$(COMPOSE) up -d ollama

clean: ## system prune + remove dangling volumes (keeps the swarm's own data volumes)
	./scripts/clean.sh

disk-usage: ## container disk usage (images, containers, volumes, build cache)
	$(RUNTIME) system df

bootstrap: ## schema + JetStream streams/consumers + dev secrets + Apicurio schemas
	./scripts/bootstrap.sh

swarm: ## start the containerized relay + all four role agents
	$(COMPOSE) --profile swarm up -d --build

forgejo: ## self-hosted git + Actions (GPLv3, isolated service)
	$(COMPOSE) --profile forgejo up -d forgejo

chat: ## Telegram/Slack/Discord ingress (CHAT_SOURCES in compose.yaml)
	$(COMPOSE) --profile chat up -d --build

vllm: ## Tier-A vLLM (inference upgrade past ~5 concurrent agents; GPU)
	$(COMPOSE) --profile vllm up -d

qdrant: ## optional vector-store upgrade (then VECTOR_BACKEND=qdrant)
	$(COMPOSE) --profile qdrant up -d

observability: ## OpenSearch logs · Jaeger traces · Prometheus + Perses dashboards · Langfuse LLM traces
	$(COMPOSE) --profile observability up -d

PERSES_URL ?= http://localhost:8090
perses-migrate: ## convert grafana-sla.json with percli against the compose Perses (refinement aid)
	percli login $(PERSES_URL)
	percli migrate -f deploy/observability/grafana-sla.json --online -o json

openobserve: ## optional single-binary observability swap
	$(COMPOSE) --profile openobserve up -d

headroom: ## token-compression proxy sidecar (v4.1; seed assets first)
	$(COMPOSE) --profile headroom up -d --build

litellm: ## model gateway: local primary, opt-in API fallback (v4.1)
	$(COMPOSE) --profile litellm up -d

strix: ## Strix security lane (v4.2; augments bandit, opt-in): the Strix CLI from the locked `security` group — STRIX_SCAN_MODE, STRIX_TARGET
	./scripts/strix_quick_scan.sh

codex-engine: ## Codex engine lane (v4.2 T7.3; if not egress-sealable)
	$(COMPOSE) --profile codex up -d --build

relay: ## run the outbox relay on the host (needs node 22: cd relay && npm install)
	cd relay && npm start

relay-cdc: ## CDC relay option: tail logical replication instead of polling
	cd relay && npm run start:cdc

agent: ## run one agent worker on the host (AGENT_ROLE=backend|frontend|review|triage)
	AGENT_ROLE=$(or $(ROLE),backend) python -m agent.agent_worker

graph: ## LangGraph orchestration option (HIL=1 to pause before work)
	AGENT_ROLE=$(or $(ROLE),backend) python -m agent.orchestrator_langgraph

crew: ## CrewAI role-modeling option (declarative planning crew)
	python -m agent.crew_roles "$(or $(TITLE),demo: add a /healthz endpoint)"

ingest: ## one issue-sync pass (Forgejo swarm-ready issues -> tasks)
	python -m ingest.forgejo_issue_sync

ingest-container: ## build the ingest image (headroom-ai inside) and run one issue sync in it
	$(COMPOSE) --profile chat build chat-ingress
	$(COMPOSE) --profile chat run --rm chat-ingress python -m ingest.forgejo_issue_sync

tools: ## isolated CLI tools: headroom (learn loop), pinned by uv.lock — never installed into the agent env
	uv tool install --constraints requirements-headroom-proxy.txt headroom-ai==0.31.0

inject: ## inject a synthetic task through the outbox
	python scripts/inject_task.py --role backend --title "synthetic task"

qualify: ## stage-1 qualification checks (after: inject_task.py --count 100)
	python scripts/qualify_stage1.py

dlq: ## DLQ -> human triage pass (Stage 4 requirement)
	python scripts/dlq_triage.py

dlq-replay: ## re-inject a fixed task by stream sequence (SEQ=<n>)
	python scripts/dlq_triage.py --replay $(SEQ)

sla: ## operational SLA report (completion, latency, green rate, headroom savings)
	python scripts/sla_report.py

seed: ## seed vector memory from knowledge/ (Appendix C packs)
	python scripts/seed_knowledge.py

index-repo: ## RAG-index a repo's files + signatures (REPO=/path)
	python scripts/seed_knowledge.py --index-repo $(or $(REPO),.)

shadow-eval: ## compare a candidate config vs default on the golden pack
	python scripts/shadow_eval.py $(foreach kv,$(CANDIDATE),--candidate $(kv))

shadow-eval-engines: ## cross-engine eval: aider (default) vs codex (v4.2 T7.6)
	env AGENT_ENGINE=aider python scripts/shadow_eval.py --single engine-aider
	env AGENT_ENGINE=codex python scripts/shadow_eval.py --single engine-codex
	python scripts/shadow_eval.py --report eval/engine-aider.json eval/engine-codex.json

morning: ## Morning Operator Routine: dlq + sla + verifier audit + learn
	./scripts/morning_routine.sh

speculative: ## multi-candidate patch fan-out for a swarm-hard task (TASK=<id>)
	python scripts/speculative_patch.py $(TASK)

verify-diff: ## independent verifier over a diff+spec (DIFF=<f> SPEC=<f>)
	python -m agent.verifier --diff $(DIFF) --spec $(SPEC)

codex-review: ## Codex as a 2nd independent reviewer over a diff (v4.2 T7.4)
	python -m agent.codex_review --base $(or $(BASE),integration)

codex-audit: ## Codex offline egress audit — sealed vs lane (v4.2 T7.1)
	./scripts/codex_egress_audit.sh

agents-md-check: ## AGENTS.md <= 32 KiB cross-tool constitution (v4.2 T7.5)
	python .forgejo/scripts/agents_md_gate.py

escalation: ## escalation lane (v4.2 T7.7; R11): HOST-RUN worker for swarm-hard tasks handed off after repair — needs ROUTE_API_FALLBACK=1 and the Claude key in OpenBao; never in a container or an image
	./scripts/escalation_lane.sh

strix-scan: ## same lane as `make strix` (one script): a quick scan of this repo; needs Docker + the model gateway
	./scripts/strix_quick_scan.sh

strix-findings: ## route a Strix JSON report -> lessons + issues (REPORT=<f>)
	python scripts/strix_findings.py --report $(REPORT)

doomloop: ## measure Ornith doom-loop + snowball rate (v4.2 T9.2)
	python scripts/doomloop_probe.py --snowball-rate

migrate: ## apply db/migrations/*.sql in order (idempotent; optional/ is applied by hand)
	for f in db/migrations/*.sql; do \
	  $(RUNTIME) exec -i $$($(RUNTIME) ps -qf name=postgres | head -1) \
	    psql -v ON_ERROR_STOP=1 -U postgres -d swarm < $$f || exit 1; done

EMBED_MODEL ?= all-minilm
models: ## pull the default Ornith model and the embedding model (EMBED_MODEL) into the ollama container
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-9b:Q4_K_M
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull $(EMBED_MODEL)

models-35b: ## 35B MoE Q4 for 24GB+ nodes (both architect and editor roles)
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-35b:Q4_K_M

models-bf16: ## 9B bf16 for 48-80GB nodes (T4.5 lineup; higher fidelity)
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-9b:bf16

test:
	pytest -q

lint:
	ruff check .

ci-local: ## the gates of .forgejo/workflows/ci.yaml, same commands (license gate deferred without syft)
	./scripts/ci_local.sh

hooks: ## install the developer pre-commit hooks (ruff, ruff-format, uv-lock, hygiene)
	uv run --no-sync pre-commit install

gate: ## license gate zones C (fail-closed) + B (denylist), Zone A table (needs syft)
	python .forgejo/scripts/license_gate.py --zone C
	python .forgejo/scripts/license_gate.py --zone B
	python .forgejo/scripts/license_gate.py --zone A

gen: ## regenerate protobuf code from proto/ (needs grpcio-tools; or use buf)
	python -m grpc_tools.protoc -Iproto \
	  --python_out=agent/gen --pyi_out=agent/gen \
	  proto/swarm/v1/task.proto proto/swarm/v1/event.proto

logs:
	$(COMPOSE) logs -f --tail=100

footprint: ## wheel sizes per requirement set (+ image sizes when a runtime is up)
	./scripts/footprint_report.sh

GROUPS ?= agent dev
EXTRAS ?=

lock: ## uv lock + regenerate every requirements*.txt from uv.lock (idempotent)
	./scripts/lock_export.sh

sync: ## uv sync --frozen GROUPS="agent dev" EXTRAS="embeddings" (never agent + ingest)
	uv sync --frozen $(foreach g,$(GROUPS),--group $(g)) $(foreach e,$(EXTRAS),--extra $(e))

torch-cpu-lock: ## enable CPU torch wheels for the torch extras and re-lock (needs download.pytorch.org)
	./scripts/torch_cpu_lock.sh

# Every check that needs a runtime host (Podman, GPU, the running spine).
# Tasks that cannot prove their acceptance in a dev session append their
# exact command here; deferred is not skipped — this target must pass on
# the owner's runtime host. Each step runs through scripts/vi_step.sh (one
# failure never hides the rest); scripts/vi_summary.sh fails at the end.
VI := ./scripts/vi_step.sh
verify-integration: ## run every deferred integration check (runtime host)
	@$(VI) --reset
	@$(VI) "L0.1 footprint (wheels + images + disk + SLA latency)" $(MAKE) --no-print-directory footprint
	@$(VI) "L0.2/L0.4/L1.4/R1 images build within size targets" ./scripts/image_sizes.sh
	@$(VI) "R1 Headroom proxy image starts: /health 200" ./scripts/headroom_image_smoke.sh
	@$(VI) "L1.1 CPU-only torch for the torch extras" $(MAKE) --no-print-directory torch-cpu-lock
	@$(VI) "L1.2/L3.1/R2 license gate zone C (publisher metadata + syft over code and the npm lock, fail-closed)" uv run --no-sync python .forgejo/scripts/license_gate.py --zone C
	@$(VI) "L3.1/R2 license gate zone B (publisher metadata + syft over the repository, denylist)" uv run --no-sync python .forgejo/scripts/license_gate.py --zone B
	@$(VI) "L1.5/R5 lite spine: host reaches the ports, spine has no egress (HOSTPORTS=1 only if needed), one task reaches done" ./scripts/lite_smoke_modes.sh
	@$(VI) "L2.2+ real-Postgres tests (hybrid retrieval SQL) on the spine" env REQUIRE_POSTGRES=1 uv run --no-sync pytest -q
	@$(VI) "L2.3 fastembed option embeds: embed('hello') is a 384-vector" env EMBEDDING_BACKEND=fastembed EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 uv run --frozen --group agent --extra embeddings python -c "from agent.lessons import embed; v = embed('hello'); assert v is not None and len(v) == 384, v; print('384-dim ok')"
	@$(VI) "R4 api embedder against the spine's Ollama: embed('hello') is a 384-vector" env EMBEDDING_BACKEND=api EMBEDDING_API_BASE=http://localhost:11434/v1 EMBEDDING_MODEL=$(EMBED_MODEL) uv run --no-sync python -c "from agent.lessons import embed; v = embed('hello'); assert v is not None and len(v) == 384, v; print('384-dim ok')"
	@$(VI) "L2.3/L3.3/R4 shadow-eval, full ten-task golden pack per embedder: api default and fastembed first-pass green rate >= the v4.1 baseline" ./scripts/embeddings_shadow.sh
	@$(VI) "L3.2 observability: gauges scraped, task span in Jaeger, Perses dashboard" ./scripts/observability_smoke.sh
	@$(VI) "L3.2 percli lint of the Perses dashboard (needs percli)" percli lint -f deploy/observability/perses-sla.json
	@$(VI) "R7 v4.2 Codex lane image builds (the build itself runs codex --version)" $(RUNTIME) build -f deploy/Containerfile.codex -t swarm-codex:dev .
	@$(VI) "R7 v4.2 Codex offline-egress audit (the lane image on the internal network; needs the spine's Ollama with a model)" ./scripts/codex_egress_audit.sh
	@$(VI) "R7 v4.2 Strix quick scan from the locked security group (needs Docker + the model gateway)" ./scripts/strix_quick_scan.sh
	@$(VI) "R11 v4.2 real escalation run: staged hand-off -> lane -> gate -> feature/* branch (SKIP unless ROUTE_API_FALLBACK=1 and the Claude key lease succeeds; needs the spine with the relay and Forgejo)" ./scripts/escalation_lane.sh --smoke
	@./scripts/vi_summary.sh
