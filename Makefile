# Overnight Agent Swarm v4.0 — Tier A developer entry points.
# `make quickstart` = up + bootstrap.

RUNTIME := $(shell command -v podman || command -v docker)
COMPOSE := $(RUNTIME) compose -f deploy/compose.yaml
COMPOSE_LITE := $(RUNTIME) compose -f deploy/compose.lite.yaml

.PHONY: quickstart up down bootstrap swarm forgejo chat vllm qdrant \
        observability openobserve headroom litellm relay relay-cdc agent \
        graph crew verify-diff ingest inject qualify dlq dlq-replay sla seed \
        index-repo shadow-eval morning speculative test lint gate gen \
        models models-35b models-bf16 migrate logs footprint \
        verify-integration tools ingest-container lock sync torch-cpu-lock \
        ci-local hooks up-lite down-lite ollama clean disk-usage perses-migrate

quickstart: up bootstrap ## start infra and bootstrap everything

up: ## start the Tier A spine (nats, postgres, apicurio, valkey, seaweedfs, openbao, ollama)
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

up-lite: ## lite spine: nats, postgres+pgvector, valkey, openbao (no apicurio/seaweedfs/ollama)
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

morning: ## Morning Operator Routine: dlq + sla + verifier audit + learn
	./scripts/morning_routine.sh

speculative: ## multi-candidate patch fan-out for a swarm-hard task (TASK=<id>)
	python scripts/speculative_patch.py $(TASK)

verify-diff: ## independent verifier over a diff+spec (DIFF=<f> SPEC=<f>)
	python -m agent.verifier --diff $(DIFF) --spec $(SPEC)

migrate: ## apply db/migrations/*.sql in order (idempotent; optional/ is applied by hand)
	for f in db/migrations/*.sql; do \
	  $(RUNTIME) exec -i $$($(RUNTIME) ps -qf name=postgres | head -1) \
	    psql -v ON_ERROR_STOP=1 -U postgres -d swarm < $$f || exit 1; done

models: ## pull the default Ornith model into the ollama container
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-9b:Q4_K_M

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
	@$(VI) "L1.2/L3.1 license gate zone C (syft, fail-closed)" uv run --no-sync python .forgejo/scripts/license_gate.py --zone C
	@$(VI) "L3.1 license gate zone B (syft, denylist)" uv run --no-sync python .forgejo/scripts/license_gate.py --zone B
	@$(VI) "L1.5 lite spine: boot, bootstrap, one task reaches done" ./scripts/lite_smoke.sh
	@$(VI) "L2.2+ real-Postgres tests (hybrid retrieval SQL) on the spine" env REQUIRE_POSTGRES=1 uv run --no-sync pytest -q
	@$(VI) "L2.3 fastembed embeds: embed('hello') is a 384-vector" uv run --frozen --group agent --extra embeddings python -c "from agent.lessons import embed; v = embed('hello'); assert v is not None and len(v) == 384, v; print('384-dim ok')"
	@$(VI) "L2.3/L3.3 shadow-eval, full ten-task golden pack per embedder: fastembed first-pass green rate >= baseline" ./scripts/embeddings_shadow.sh
	@$(VI) "L3.2 observability: gauges scraped, task span in Jaeger, Perses dashboard" ./scripts/observability_smoke.sh
	@$(VI) "L3.2 percli lint of the Perses dashboard (needs percli)" percli lint -f deploy/observability/perses-sla.json
	@./scripts/vi_summary.sh
