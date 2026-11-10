# OWNER_STOP — what only the owner can do

The Lean & Precise loop and its review round (R1–R11) stop here: v4.1 →
v4.2.0. Everything a dev session can prove is proven and recorded in
[STATE.md](STATE.md). The six steps below need your credentials or your
runtime host. They are one batch, in order; none asks for a decision.
What you may want to look at afterwards is in "For your information".

## 1. Merge the pull request

PR #3, `feature/lean-precise` → `main`, with a **merge commit** (GitHub:
"Create a merge commit" — not squash, not rebase). The history is one
commit per task (`lean(<task-id>): …`), each with its files and evidence,
and the tags of step 2 point into it.

## 2. Recreate and push the tags

The session's git proxy refuses tag pushes, so the tags exist only in the
session container. After the merge:

```bash
git fetch origin
git tag lean-phase-0 af5dcd3
git tag lean-phase-1 6ab3dfd
git tag lean-phase-2 22bd2ef
git tag lean-phase-3 056a89c
git tag lean-review-round R8_COMMIT
git push origin lean-phase-0 lean-phase-1 lean-phase-2 lean-phase-3 lean-review-round
```

## 3. Run the integration checks on the runtime host

On the Podman / GPU / spine host, in a checkout of the merged `main`:

```bash
# once — the two tools the dev session could not install or run:
#   syft    https://github.com/anchore/syft     (license gate: code directories, both npm locks, repository)
#   percli  https://github.com/perses/perses    (dashboard lint; v0.55.1, the pinned Perses)
uv sync --frozen --group agent --group dev
export LITE_SMOKE_FORGEJO_TOKEN=<the Forgejo bot token of step 5>
make verify-integration
```

It runs every deferred check, never stops at the first failure, prints a
PASS / FAIL / SKIP table and the network mode this host needs, and exits
non-zero if any step failed. Then commit what it produced:

```bash
git add docs/footprint-baseline.md      # paste the `make footprint` output as a new dated section
git add -f eval/last_report.md          # git-ignored since v4.1, hence -f
git add pyproject.toml uv.lock requirements*.txt   # only if the torch-cpu-lock step passed
git commit -m "lean(integration): record verify-integration results"
git push
```

The steps, in order:

| step | proves | needs |
|---|---|---|
| footprint | wheel table, image sizes, `system df`, SLA latency lines | container runtime |
| images within size targets | agent ≤ 850 MB, ingest ≤ 450 MB, relay ≤ 160 MB, headroom ≤ 760 MB | container runtime |
| Headroom proxy image starts | the built image answers `/health` with 200 | container runtime |
| `make torch-cpu-lock` | the torch extras resolve to CPU-only wheels (removes the NVIDIA wheels from the lock) | download.pytorch.org |
| license gate zone C, zone B | Python from pypi.org metadata (already green in the session; a `+cpu` torch from the step above is judged by its public version) plus syft over the code directories, `relay/package-lock.json`, `deploy/codex/package-lock.json` and the repository | syft, pypi.org |
| lite spine | host reaches 5432 / 4222 / 6379 / 8200; no outbound connection from the postgres container; one task reaches `done`. Runs the default network mode first and `HOSTPORTS=1` only if the ports are unreachable | Ollama models, Forgejo with a `swarm/sandbox-repo` the bot can push `feature/*` to, the token |
| real-Postgres tests | hybrid retrieval, embedder isolation, codex index, retention, SLA queries, the escalation hand-off and lane, migration 011 — on the spine's pgvector image | spine up |
| fastembed option | `embed('hello')` is a 384-vector in process | huggingface.co or a seeded cache |
| api embedder | `embed('hello')` is a 384-vector from the spine's Ollama | `all-minilm` pulled (the lite smoke does it) |
| embedder shadow-eval | ten-task golden pack per embedder; the api default and fastembed are at least as green as the v4.1 baseline; writes `eval/last_report.md` | all of the above + torch for the baseline run |
| observability smoke | every SLA gauge scraped, a task span in Jaeger, the Perses dashboard loaded | spine + swarm profile |
| `percli lint` | the Perses dashboard validates | percli |
| Codex lane image | `deploy/Containerfile.codex` builds from the two locks; the build runs `codex --version` and fails if a bundled bubblewrap or voice runtime is left in the image | container runtime, registry.npmjs.org |
| Codex egress audit | Codex completes a task on the internal network with no callout, with the swarm's model and the offline profile | the image above, the spine's Ollama (≥ 0.13.4) with the Ornith model |
| Strix quick scan | Strix runs from the locked `security` group; a completed scan passes whatever it finds, and findings become lessons | Docker, `ghcr.io/usestrix/strix-sandbox:1.3.0`, the model gateway (`make litellm`) |
| real escalation run | a staged hand-off goes through the lane: SDK call, patch, gate, `feature/*` branch, `PR_OPENED escalated=1` | **SKIP** unless `ROUTE_API_FALLBACK=1` and a Claude key is at OpenBao `secret/swarm/claude` (`api_key`); then the spine with the relay, and Forgejo |

If the summary prints `host network mode: HOSTPORTS=1`, start the stack on
that host with `make up HOSTPORTS=1` / `make up-lite HOSTPORTS=1` from then on.

## 4. Forgejo runner allow-list and branch protection

- **Egress allow-list for the Forgejo Actions runner** (jobs `gates`,
  `proto-compat`, `strix`, and the image builds): `ghcr.io` (astral-sh/uv,
  the Strix sandbox image), `gcr.io` (distroless), `registry.npmjs.org`,
  `pypi.org` (packages and the license gate's metadata lookups),
  `files.pythonhosted.org`, `download.pytorch.org`, `github.com`
  (pre-commit hook repositories, the syft release), `raw.githubusercontent.com`
  (the syft installer the `gates` job fetches), and your registry mirror
  for `python:3.12-*` / `node:22-slim`.
- **Branch protection** on `integration` and `main`: required checks
  `gates` and `proto-compat`. The `security` workflow (Strix) is advisory
  and is not a required check; it prints its findings in the job log.
- GitHub shows no check runs on PR #3: CI is Forgejo Actions only, by
  canon. The same gates ran in the session with `make ci-local`.

## 5. Confirm the Forgejo bot token

The token at OpenBao `secret/data/swarm/forgejo` must be valid for the bot
account (pushes `feature/*` only):

```bash
curl -s -H "X-Vault-Token: $OPENBAO_TOKEN" "$OPENBAO_ADDR/v1/secret/data/swarm/forgejo" \
  | jq -r '"Authorization: token " + .data.data.token' \
  | curl -s -o /dev/null -w '%{http_code}\n' -H @- "$FORGEJO_URL/api/v1/user"
# expect 200 (the token travels on stdin, never on a command line)
```

`make bootstrap` keeps a stored token; a fresh dev-mode OpenBao starts
empty and gets a placeholder (`REPLACE_WITH_FORGEJO_BOT_TOKEN`). Nothing in
the PR contains a secret.

## 6. Only if a v4.1 database exists: upgrade it

```bash
# 6a. check the embedding column — the old `make migrate` also applied the
#     optional bge-m3 switch, which resized it to 1024 and cleared it
psql "$DATABASE_URL" -c '\d lessons' | grep embedding
```

If it shows `vector(1024)` and you do not run bge-m3 (`EMBEDDING_DIM=1024`),
switch back first:

```sql
DROP INDEX IF EXISTS lessons_embedding_idx;
ALTER TABLE lessons ALTER COLUMN embedding TYPE vector(384) USING NULL;
CREATE INDEX lessons_embedding_idx ON lessons USING hnsw (embedding vector_cosine_ops);
```

Then, in this order:

```bash
make migrate                                  # 002–011; never the optional bge-m3 switch; stops on the first error
make bootstrap                                # idempotent; keeps stored secrets; adds the `escalation` consumer
make models                                   # the Ornith model and the embedding model (all-minilm)
python scripts/seed_knowledge.py --reembed    # stored vectors only match once they carry the current embedder
```

`--reembed` refuses to run when the embedder is unreachable and never
clears a vector. `make sla` then shows one embedder for all lessons.

---

## For your information

Nothing here blocks the six steps. These are decisions the loop took that
you can reverse, things it found and did not change, and things it could
not verify.

**The three v4.2 capabilities are wired, and off** (`conventions/escalation.md`)

| to turn on | what it needs |
|---|---|
| Escalation — `ROUTE_API_FALLBACK=1` for the agents, and `ROUTE_API_FALLBACK=1 make escalation` on a host | a Claude key at OpenBao `secret/swarm/claude` (`api_key`); issues labelled `swarm-hard`; `make migrate` and `make bootstrap` (step 6). The lane is host-run by design: it is in no image |
| Rubric — `RUBRIC_ENABLED=1` | nothing else; one more model call per task |
| Codex as fourth reviewer — `CODEX_REVIEW=1` | a `codex` binary where the worker runs (the Codex lane image has it; the agent image does not, and reports that as a non-blocking ERROR) |

**Decided in the loop — reverse if you disagree**

| what | where | to reverse |
|---|---|---|
| The escalation lane's gate runs model-written code **on the host**, outside the container seal. Its environment is an allowlist (no swarm secret, no service address) and the bot token is not in the clone, but it is not sandboxed | D-044 (g), D-047 (b) | run `make escalation` only on a host or VM where that is acceptable, or leave `ROUTE_API_FALLBACK=0` |
| With `RUBRIC_ENABLED=1` or `CODEX_REVIEW=1` a pre-push review step exists. A `swarm-hard` task then also gets the three verifier lenses, and a BLOCK or a lens ERROR stops the push. v4.2 had no point in the flow that consumed the verifier's verdict | D-044 (a) | — (off by default) |
| Issue labels are stored on the task row (`tasks.labels`); `swarm-hard` is the label or `[swarm-hard]` in the title | D-044 (b) | — |
| `AGENT_ENGINE=codex` runs Codex with its own sandbox off (`--sandbox danger-full-access`): that sandbox needs user namespaces the hardened containers do not give, and its default cannot edit files. The container is the boundary, as for aider | D-046 (b) | set `CODEX_EXEC_FLAGS` |
| Codex review runs `codex exec --output-schema` with the diff in the prompt, not `codex exec review`: the latter has no machine-readable result, so nothing in it can say PASS | D-046 (f) | — |
| The Codex lane image drops two LGPL components the upstream package bundles (bubblewrap, a voice runtime) | D-046 (h) | remove the `rm` line and the check in `deploy/Containerfile.codex`, and add an acknowledgement to `conventions/licensing.md` |
| The Strix lane is the upstream CLI (`make strix`); the compose service and the Kubernetes Job are gone. A completed scan is exit 0 whatever it finds; `STRIX_FAIL_ON` makes it blocking | D-043, D-045 (c) | — |
| The license gate accepts lock entries from PyPI and from PyTorch's CPU index only; anything else (git, URL, path, another registry) is exit 2 | D-045 (a), (b) | add the index to `ALTERNATE_INDEXES` in `.forgejo/scripts/license_gate.py` |
| `MIT-0` and `Zlib` joined the license allowlist, beyond the `MIT-CMU` and `CNRI-Python` you named. The headroom image's closure needs them (cffi 2.1.1, numpy 2.5.4) | D-036 | remove the two ids from `ALLOWED`; the gate then blocks the headroom image |
| Tier B gets `deploy/k8s/embeddings.yaml`, a small CPU Ollama serving `all-minilm`. Its one-time model pull needs `deploy/k8s/embeddings-model-pull.yaml` applied and deleted around it (the commands are in the manifest's header) | D-038, D-045 (h) | point `EMBEDDING_API_BASE` in the k8s agent manifests at another OpenAI-compatible embeddings server |
| Diff-size gate is reported, not enforced, for PRs into `main` (enforced on `integration`) | D-013 | `DIFF_SIZE_ENFORCE=1` in the workflow |

**Found, not changed**

- **A poisoned task does not reach `SWARM_DLQ`.** The worker ends it with
  `term()`. JetStream then publishes a `MSG_TERMINATED` advisory; the
  `SWARM_DLQ` stream subscribes only to `MAX_DELIVERIES`. So `make dlq`
  (and the needs-human issue it opens) sees tasks that exhaust
  `max-deliver` — crash loops, repeated transient failures — and not the
  tasks the gate, the breaker, the review step or the escalation lane
  fail. Those are recorded: `tasks.status = 'failed'` and a `FAILED` event
  with the reason. This is how the normal path has behaved since v4.0; the
  session has no NATS to prove a change. To check on your host: poison a
  task and watch `nats sub '$JS.EVENT.ADVISORY.CONSUMER.>'`. If you want
  them in triage, the options are to add
  `$JS.EVENT.ADVISORY.CONSUMER.MSG_TERMINATED.SWARM_TASKS.*` to the stream
  and teach `scripts/dlq_triage.py` that the terminated message is gone
  from the work queue, or to let triage read failed tasks from Postgres.
  (D-047 (d))
- The LangGraph orchestrator option calls the sandbox the old way: the
  three capabilities are not wired there (D-044 (h)).
- `codex-engine` sits on the `egress` network, as v4.2 designed the lane.
  With Codex's sandbox off, model-run commands there can reach the
  internet. The egress audit decides whether Codex can run sealed instead.

**Not verified — no way to check from the session**

- Nothing that needs a container runtime ran: image builds, both network
  modes, the smokes. `docker compose config` validated every file
  combination; that is all.
- **Codex was never executed.** Flags, defaults, event shapes, config keys
  and exit codes were read from `openai/codex` at tag `rust-v0.167.0` and
  re-checked by a second reader. Open: whether Ollama lists the model
  exactly as `maxwell1500/ornith-9b:Q4_K_M`, whether it honours the review
  output schema (if not, the review is an ERROR, never a PASS), and
  whether anything else in Codex calls out — `make codex-audit` decides.
- The Codex platform package's contents were not listed (a binary
  download was declined). That it bundles bubblewrap and a voice runtime
  is from upstream's packaging scripts; the image build checks by file name.
- **The Claude Agent SDK was never called.** Option and message names were
  read from `claude-agent-sdk` 0.2.180. Its wheel bundles the Claude Code
  CLI, which is under Anthropic's Commercial Terms; it is in no image.
- Strix never scanned anything here. Its flags, exit codes and the sandbox
  image tag are from the installed strix-agent 1.7.0; the image itself was
  not pulled.
- `deploy/antidoom/compose.yaml` and the temporary Tier B egress policy
  were corrected by reading, not by running.
- The `HOSTPORTS=1` network options are not a firewall (D-039): the lite
  smoke proves the two properties that matter on the host it runs on.
- syft has never run against this tree. The Python verdict no longer
  depends on it; the two npm locks and the code directories do.
