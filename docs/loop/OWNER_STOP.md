# OWNER_STOP — what only the owner can do

The Lean & Precise loop (v4.1 → v4.1.1) stops here. Everything a dev
session can prove is proven and recorded in [STATE.md](STATE.md); the steps
below need your credentials, your runtime host or your decision.

## 1. Merge the pull request

PR `feature/lean-precise` → `main` (#3). Use a **merge commit**, not a
squash: the history is one commit per task (`lean(<task-id>): …`), each
with its files and acceptance evidence, and the phase tags point into it.

Not in this PR: the v4.2 "Elevation+" work (commit `ce2edd9` on
`claude/agent-swarm-repo-taafee`) was never merged to `main`, so this loop
was built on v4.1 (DECISIONS D-001). Rebase v4.2 onto `main` after this
merge if you still want it.

## 2. Push the phase tags

The session's git proxy refuses tag pushes (D-015), so the tags exist only
in the session container. Recreate and push them after the merge:

```bash
git tag lean-phase-0 af5dcd3
git tag lean-phase-1 6ab3dfd
git tag lean-phase-2 22bd2ef
git tag lean-phase-3 <the "lean(phase-3): close out" commit — see STATE.md>
git push origin lean-phase-0 lean-phase-1 lean-phase-2 lean-phase-3
```

## 3. Run the integration checks on the runtime host

On the Podman / GPU / spine host:

```bash
# once: tools the dev session could not install or run
#   syft    https://github.com/anchore/syft     (license gate scans)
#   percli  https://github.com/perses/perses    (dashboard lint, v0.55.1)
uv sync --frozen --group agent --group dev
export LITE_SMOKE_FORGEJO_TOKEN=<Forgejo bot token>   # see step 6
make verify-integration
```

It runs every deferred check and prints a PASS/FAIL table; it exits
non-zero if any step fails. The steps, in order:

| step | what it proves | needs |
|---|---|---|
| L0.1 footprint | wheel table + image sizes + `system df` + SLA latency lines | container runtime |
| L0.2/L0.4/L1.4 image sizes | agent ≤ 700 MB, ingest ≤ 300 MB, relay ≤ 160 MB | container runtime |
| L1.1 `make torch-cpu-lock` | torch extras resolve to CPU-only wheels | download.pytorch.org |
| L1.2/L3.1 license gate zone C, zone B | real syft scans | syft |
| L1.5 lite smoke | lite spine boots, bootstrap, one task reaches `done` | Ollama model, Forgejo with a `swarm/sandbox-repo` the bot can push `feature/*` to, `LITE_SMOKE_FORGEJO_TOKEN` |
| L2.2+ real-Postgres tests | hybrid retrieval, codex index, retention, SLA queries on the spine's pgvector image (`REQUIRE_POSTGRES=1`) | spine up |
| L2.3 `embed("hello")` | fastembed downloads bge-small and returns a 384-vector | huggingface.co (or a pre-seeded cache) |
| L2.3/L3.3 embedder shadow-eval | ten-task golden pack per embedder; fastembed first-pass green rate ≥ the v4.1 baseline | all of the above + torch for the baseline run |
| L3.2 observability smoke | every SLA gauge scraped, a task span in Jaeger, the Perses dashboard loads | spine + swarm profile |
| L3.2 `percli lint` | the Perses dashboard validates | percli |

Then commit the results:

```bash
git add docs/footprint-baseline.md          # paste the `make footprint` output as a new dated section
git add -f eval/last_report.md              # gitignored since v4.1 — hence -f
git add pyproject.toml uv.lock requirements*.txt   # only if torch-cpu-lock succeeded
git commit -m "lean(integration): record verify-integration results"
```

Expect two honest failures unless you decide otherwise (step 5): the agent
image is forecast at ≈ 755 MB against the 700 MB target and the ingest
image above 300 MB.

## 4. CI runner and branch protection

- **Egress allow-list for the Forgejo Actions runner** (the `gates` and
  `proto-compat` jobs and the image builds):
  `ghcr.io` (astral-sh/uv), `gcr.io` (distroless), `registry.npmjs.org`,
  `pypi.org`, `files.pythonhosted.org`, `download.pytorch.org`,
  `github.com` (pre-commit hook repositories and the syft release, D-016),
  `raw.githubusercontent.com` (the syft installer the `gates` job fetches),
  and your container registry mirror for `python:3.12-*` / `node:22-slim`.
- **Branch protection** on `integration` and `main`: required checks
  `gates` and `proto-compat`.
- The repository on GitHub has no CI of its own (Forgejo Actions only, by
  canon) — PR #3 shows no check runs there. The same gates ran locally with
  `make ci-local`; they run for real on your Forgejo.

## 5. Decisions that are yours

| decision | where | current state |
|---|---|---|
| Diff-size gate is **reported, not enforced**, for PRs into `main` (enforced on `integration`, the agent path) | D-013 | veto by setting `DIFF_SIZE_ENFORCE=1` in the workflow |
| Agent image target 700 MB vs forecast ≈ 755 MB — raise the target, or prune tree-sitter grammars / scipy in a follow-up | D-028 | target kept, so the check fails honestly |
| Ingest image target 300 MB vs a 236 MB venv before the base image | D-017 | target kept |
| Spine services publish host ports while attached only to an `internal: true` network — Docker may not publish them | D-029(g) | unchanged from v4.0; the lite smoke shows whether it bites on your runtime |
| One database indexes one repository (`codex-index` rows are keyed by relative path) | D-025(c) | as specified |
| `make clean` keeps the swarm's own data volumes; `make forgejo` now starts only Forgejo | D-020 | — |

## 6. Secrets

- Confirm the Forgejo bot token at OpenBao `secret/data/swarm/forgejo` is
  valid. `make bootstrap` no longer overwrites a stored token with the
  placeholder (it used to); a fresh dev-mode OpenBao still starts empty.
- The lite smoke stores `LITE_SMOKE_FORGEJO_TOKEN` at that path itself.
- Nothing in this PR contains a secret; placeholders are
  `REPLACE_WITH_<NAME>_BOT_TOKEN`.

## 7. Upgrading an existing v4.1 deployment

```bash
make migrate                                   # 006 gate timing, 007 lessons FTS + hybrid search,
                                               # 008 codex source/sha, 009 task tokens
make bootstrap                                 # idempotent; keeps stored secrets
python scripts/seed_knowledge.py --reembed     # default embedder changed (not needed if your .env
                                               # pins EMBEDDING_MODEL=all-MiniLM-L6-v2 — same vectors)
make index-repo REPO=<path>                    # rebuild the signature index (now multi-language)
```

Hosts without egress that install the `embeddings` extra must pre-seed the
fastembed model cache (`FASTEMBED_CACHE_PATH`); without the model, lessons
degrade to text-only search and an error is logged. Agent images do not
install the extra.
