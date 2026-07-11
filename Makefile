# Overnight Agent Swarm v4.0 — Tier A developer entry points.
# `make quickstart` = up + bootstrap.

RUNTIME := $(shell command -v podman || command -v docker)
COMPOSE := $(RUNTIME) compose -f deploy/compose.yaml

.PHONY: quickstart up down bootstrap swarm observability relay agent ingest \
        inject qualify test lint gate gen models logs

quickstart: up bootstrap ## start infra and bootstrap everything

up: ## start the Tier A spine (nats, postgres, apicurio, valkey, seaweedfs, openbao, ollama)
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

bootstrap: ## schema + JetStream streams/consumers + dev secrets + Apicurio schemas
	./scripts/bootstrap.sh

swarm: ## start the containerized relay + backend agent
	$(COMPOSE) --profile swarm up -d --build

observability:
	$(COMPOSE) --profile observability up -d

relay: ## run the outbox relay on the host (needs node 22: cd relay && npm install)
	cd relay && npm start

agent: ## run one agent worker on the host (AGENT_ROLE=backend|frontend|review|triage)
	AGENT_ROLE=$(or $(ROLE),backend) python -m agent.agent_worker

ingest: ## one issue-sync pass (Forgejo swarm-ready issues -> tasks)
	python -m ingest.forgejo_issue_sync

inject: ## inject a synthetic task through the outbox
	python scripts/inject_task.py --role backend --title "synthetic task"

qualify: ## stage-1 qualification checks (after: inject_task.py --count 100)
	python scripts/qualify_stage1.py

models: ## pull the default Ornith model into the ollama container
	$(RUNTIME) exec -it $$($(RUNTIME) ps -qf name=ollama | head -1) \
	  ollama pull maxwell1500/ornith-9b:Q4_K_M

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
