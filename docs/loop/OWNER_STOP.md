# OWNER_STOP — what only the owner can do

The Lean & Precise loop and its review round (R1–R8) stop here: v4.1 →
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
#   syft    https://github.com/anchore/syft     (license gate: code directories, npm lock, repository)
#   percli  https://github.com/perses/perses    (dashboard lint; v0.55.1, the pinned Perses)
uv sync --frozen --group agent --group dev
export LITE_SMOKE_FORGEJO_TOKEN=<the Forgejo bot token of step 5>
make verify-integration
```

It runs every deferred check, never stops at the first failure, prints a
PASS/FAIL table and the network mode this host needs, and exits non-zero
if any step failed. Then commit what it produced:

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
| license gate zone C, zone B | Python from pypi.org metadata (already green in the session) plus syft over the code directories, the npm lock and the repository | syft, pypi.org |
| lite spine | host reaches 5432 / 4222 / 6379 / 8200; no outbound connection from the postgres container; one task reaches `done`. Runs the default network mode first and `HOSTPORTS=1` only if the ports are unreachable | Ollama models, Forgejo with a `swarm/sandbox-repo` the bot can push `feature/*` to, the token |
| real-Postgres tests | hybrid retrieval, embedder isolation, codex index, retention, SLA queries on the spine's pgvector image | spine up |
| fastembed option | `embed('hello')` is a 384-vector in process | huggingface.co or a seeded cache |
| api embedder | `embed('hello')` is a 384-vector from the spine's Ollama | `all-minilm` pulled (the lite smoke does it) |
| embedder shadow-eval | ten-task golden pack per embedder; the api default and fastembed are at least as green as the v4.1 baseline; writes `eval/last_report.md` | all of the above + torch for the baseline run |
| observability smoke | every SLA gauge scraped, a task span in Jaeger, the Perses dashboard loaded | spine + swarm profile |
| `percli lint` | the Perses dashboard validates | percli |
| Codex lane image | `deploy/Containerfile.codex` builds; the build runs `codex --version` | container runtime, registry.npmjs.org |
| Codex egress audit | Codex completes a task on the internal network with no callout | the image above, spine's Ollama with a model |
| Strix quick scan | Strix runs from the locked `security` group and writes a report | Docker, the model gateway (`make litellm`) |

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
  and is not a required check.
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
make bootstrap                                # idempotent; keeps stored secrets
make models                                   # the Ornith model and the embedding model (all-minilm)
python scripts/seed_knowledge.py --reembed    # stored vectors only match once they carry the current embedder
```

`--reembed` refuses to run when the embedder is unreachable and never
clears a vector. `make sla` then shows one embedder for all lessons.

---

## For your information

Nothing here blocks the six steps. These are decisions the loop took that
you can reverse, and things it could not verify.

**Decided in the loop — reverse if you disagree**

| what | where | to reverse |
|---|---|---|
| `MIT-0` and `Zlib` joined the license allowlist, beyond the `MIT-CMU` and `CNRI-Python` you named. The headroom image's closure needs them (cffi 2.1.1, numpy 2.5.4) | D-036 | remove the two ids from `ALLOWED` in `.forgejo/scripts/license_gate.py`; the gate then blocks the headroom image |
| The license override table went from 87 rows to the 6 still consulted | D-036 | — (the gate reads the publisher's classifiers itself now) |
| Tier B gets `deploy/k8s/embeddings.yaml`, a small CPU Ollama serving `all-minilm`, because the Tier B vLLM serves no embeddings route. Its model must be pulled once: `kubectl exec -n swarm deploy/embeddings -- ollama pull all-minilm` | D-038 | point `EMBEDDING_API_BASE` in the k8s agent manifests at another OpenAI-compatible embeddings server |
| Diff-size gate is reported, not enforced, for PRs into `main` (enforced on `integration`) | D-013 | `DIFF_SIZE_ENFORCE=1` in the workflow |
| `codex review` output that cannot be parsed is an ERROR, not a PASS; a v4.2 test that pinned the opposite was rewritten | D-041 | — |

**Not verified — no way to check from the session**

- Nothing that needs a container runtime ran: image builds, both network
  modes, the smokes. `docker compose config` validated every file
  combination; that is all.
- The image name `ghcr.io/usestrix/strix:latest` (compose profile
  `security`, `deploy/k8s/strix.yaml`): ghcr.io refuses an anonymous token
  for it and Strix's own repository names only `strix-sandbox`. The path
  that is pinned is `make strix-scan` (the locked `security` group).
- How Codex's real `--json` review output maps to findings: the parser has
  only seen hand-written fixtures. Until someone looks at real output,
  treat `make codex-review` as a second opinion, not a gate.
- `deploy/k8s/strix.yaml` (v4.2): a privileged DinD sidecar under a
  pod-level `runAsNonRoot`, under the default-deny egress policy, with
  nothing that fills `/work`. The compose `strix` service mounts the host's
  `docker.sock`. Both are as v4.2 wrote them.
- `grade_rubric`, `escalate` and `codex_review.review` have no caller:
  v4.2 ships them as opt-in entry points only.
- `claude-agent-sdk` (extra `escalation`): the gate reads its MIT metadata,
  but its wheel bundles the Claude Code CLI, which is under Anthropic's
  Commercial Terms. It is in no image and off by default.
- The `HOSTPORTS=1` network options are not a firewall (D-039): the lite
  smoke proves the two properties that matter on the host it runs on.
- syft has never run against this tree. The Python verdict no longer
  depends on it; the relay's npm lock and the code directories do.
