# Overnight Agent Swarm v4.0 — Tier A developer entry points.
# `make quickstart` = up + bootstrap.

RUNTIME := $(shell command -v podman || command -v docker)
COMPOSE := $(RUNTIME) compose -f deploy/compose.yaml

.PHONY: quickstart up down bootstrap swarm forgejo chat vllm qdrant \
        observability openobserve relay relay-cdc agent graph crew ingest \
        inject qualify dlq sla seed test lint gate gen models models-35b logs

quickstart: up bootstrap ## start infra and bootstrap everything

up: ## start the Tier A spine (nats, postgres, apicurio, valkey, seaweedfs, openbao, ollama)
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

bootstrap: ## schema + JetStream streams/consumers + dev secrets + Apicurio schemas
	./scripts/bootstrap.sh

swarm: ## start the containerized relay + all four role agents
	$(COMPOSE) --profile swarm up -d --build

forgejo: ## self-hosted git + Actions (GPLv3, isolated service)
	$(COMPOSE) --profile forgejo up -d

chat: ## Telegram/Slack/Discord ingress (CHAT_SOURCES in compose.yaml)
	$(COMPOSE) --profile chat up -d --build

vllm: ## Tier-A vLLM (inference upgrade past ~5 concurrent agents; GPU)
	$(COMPOSE) --profile vllm up -d

qdrant: ## optional vector-store upgrade (then VECTOR_BACKEND=qdrant)
	$(COMPOSE) --profile qdrant up -d

observability: ## baseline: OpenSearch (logs) + Langfuse (traces)
	$(COMPOSE) --profile observability up -d

openobserve: ## optional single-binary observability swap
	$(COMPOSE) --profile openobserve up -d

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

inject: ## inject a synthetic task through the outbox
	python scripts/inject_task.py --role backend --title "synthetic task"

qualify: ## stage-1 qualification checks (after: inject_task.py --count 100)
	python scripts/qualify_stage1.py

dlq: ## DLQ -> human triage pass (Stage 4 requirement)
	python scripts/dlq_triage.py

sla: ## operational SLA report (completion rate, pickup latency)
	python scripts/sla_report.py

seed: ## seed vector memory from knowledge/ (Appendix C packs)
	python scripts/seed_knowledge.py

models: ## pull the default Ornith model into the ollama container
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-9b:Q4_K_M

models-35b: ## 35B MoE for 24GB+ nodes (both architect and editor roles)
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-35b:Q4_K_M

test:
	pytest -q

lint:
	ruff check .

gate: ## run the fail-closed license gate locally (needs syft)
	python .forgejo/scripts/license_gate.py

gen: ## regenerate protobuf code from proto/ (needs grpcio-tools; or use buf)
	python -m grpc_tools.protoc -Iproto \
	  --python_out=agent/gen --pyi_out=agent/gen \
	  proto/swarm/v1/task.proto proto/swarm/v1/event.proto

logs:
	$(COMPOSE) logs -f --tail=100
