# knowledge/ — upstream knowledge packs (Appendix C)

Drop `.md`/`.txt` files here and run `python scripts/seed_knowledge.py` to
seed vector memory (and reference key files from `AGENTS.md`). The
documents recommend seeding from:

- **gglucass/headroom-desktop** — the headroom CI gate
- **openai/codex** — agentic coding-CLI patterns (tool-calling loops, diff
  application, sandboxed execution) to mine for the worker and skills
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
