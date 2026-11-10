# knowledge/ — upstream knowledge packs (Appendix C)

Drop `.md`/`.txt` files here and run `python scripts/seed_knowledge.py` to
seed vector memory (and reference key files from `AGENTS.md`). The
documents recommend seeding from:

- **chopratejas/headroom** (PyPI `headroom-ai`, Apache-2.0) — the token
  compression layer behind the CI gate, proxy sidecar, and `learn` loop
  (v4.1 ledger S1: replaces the earlier mis-wired reference to
  gglucass/headroom-desktop, a paid desktop app)
- **openai/codex** — agentic coding-CLI patterns (tool-calling loops, diff
  application, sandboxed execution) to mine for the worker and skills.
  **v4.2:** also a first-class engine (`AGENT_ENGINE=codex`, `codex exec
  --oss --local-provider ollama`) and a 2nd reviewer (`codex review`);
  install per `requirements-codex.md`, placement decided by
  `scripts/codex_egress_audit.sh` (DR-5)
- **usestrix/strix** (Apache-2.0, `strix-agent`) — autonomous pentest agent
  used as a dynamic security lane that AUGMENTS bandit (DR-6, `make strix`);
  self-hosted inference via `LLM_API_BASE` → the LiteLLM gateway
- **Liquid4All/antidoom** (Apache-2.0) — offline FTPO/LoRA trainer to
  suppress Ornith doom loops; off the runtime path, shadow-eval-gated (DR-7,
  `deploy/antidoom/`)
- **claude-agent-sdk** — Claude escalation on the API-fallback lane only
  (Claude-model-only, Anthropic egress; never sealed — DR-5)
- **SafeRL-Lab/nano-claude-code** — the lightweight agent runtime used
  alongside aider
- **multica-ai/andrej-karpathy-skills** — single-file coding-behavior
  guidelines (MIT). This one is not just seeded into memory: it is
  vendored as `karpathy-guidelines.md` at the repo root and imported into
  every agent's context (Appendix D; refresh with
  `scripts/update_karpathy.sh`)
- Platform docs: NATS JetStream (streams, durable pull consumers, ack
  policies, MAX_DELIVERIES/DLQ); Microservices.io transactional outbox +
  Postgres `FOR UPDATE SKIP LOCKED`; Apicurio 3.x (Protobuf SerDes,
  ccompat, compatibility rules); OpenBao 2.5.x (injector, Kubernetes
  auth); Istio ambient (ztunnel/waypoint, k3s CNI paths); SeaweedFS S3;
  Valkey commands; OpenSearch ingest; Langfuse OTel ingestion; aider
  architect/editor mode; and the Ornith-1.0 model cards
  (reasoning/tool-call parsers)

Fetch these on a machine *with* egress and commit the text here — agents
themselves have no internet access (Hard Rule 1), which is exactly why the
memory is pre-seeded.
