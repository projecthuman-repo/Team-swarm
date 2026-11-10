# Licensing — the three-zone model (Lean & Precise L3.1)

The swarm separates **what it talks to**, **what its code links**, and
**what it ships**. Each zone has its own rule; the license gate
(`.forgejo/scripts/license_gate.py`) enforces B and C on every PR and is
fail-closed for anything that ships.

| zone | what | rule | gate |
|---|---|---|---|
| **A** | network services the swarm calls over a socket, each in its own container (no linking, no redistribution of modified code) | any OSI-approved license | `--zone A` prints the table below, exits 0 |
| **B** | everything linked or imported by swarm code anywhere in the repo (runtime, dev and CI tooling, every lockfile) | no AGPL, SSPL, BUSL/Business Source, Commons Clause | `--zone B` (default path `dir:.`) — denylist |
| **C** | what is distributed: the contents of the images the swarm builds and the code it ships — `agent/ ingest/ relay/ skills/ scripts/`, the Python closure of each image (`uv export` from `uv.lock` for the agent, ingest and headroom images, by name and version) and the relay's npm closure (`relay/package-lock.json`) | permissive only: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC, 0BSD (+ PostgreSQL, PSF, Python-2.0, CNRI-Python, Unlicense, CC0, BlueOak, MIT-CMU, MIT-0, Zlib); anything else blocks | `--zone C` (default) — fail-closed allowlist |

**Image contents are Zone C.** Every image built from `deploy/Containerfile.*`
(agent, ingest, relay, headroom) ships only Zone C application code and
dependencies. The OS base layers they start from (`python:3.12-slim`,
`node:22-slim` builder, `gcr.io/distroless/nodejs22-debian12`) are
distribution aggregates under their own package licenses (glibc LGPL,
bash/coreutils GPL in Debian slim); they are platform, not swarm code, and
no swarm code links against a GPL library from them.

**Bundled runtime libraries.** The same reasoning covers one more case, and
only when it is written down: a third-party wheel that ships an unmodified
shared library of the GCC runtime inside it (numpy and scipy bundle
`libgfortran` and `libquadmath`). Such a wheel passes Zone C only with a
version-pinned row per library in
[Bundled-runtime acknowledgements](#bundled-runtime-acknowledgements), and
a row may carry only `GPL-3.0-or-later WITH GCC-exception-3.1` or
`LGPL-2.1-or-later`. A row clears only the family of its own license (GPL,
LGPL): if the same text also names AGPL, EPL, CDDL, EUPL, SSPL, a
proprietary or non-commercial term or Creative Commons, the package still
blocks, and the message names the family the rows do not cover. Before the
scan the gate removes five verbatim sentences of the standard GPL-3.0 text
and of the GCC Runtime Library Exception — section 13 ("Use with the GNU
Affero General Public License") among them — because a publisher who
reproduces those license texts would otherwise appear to be under the
families they merely mention. The gate prints the acknowledged packages,
and the families found in their text, on every run.

**Where the verdict comes from.** For Python, from the publisher: the gate
reads pypi.org's metadata for the exact version in `uv.lock` (cached in
`.cache/license-gate/`) and takes, in this order, the SPDX
`license_expression`; else the `license` field when it is a short SPDX
expression; else the `License ::` trove classifiers through a fixed map
("BSD License" → `BSD-2-Clause OR BSD-3-Clause`, …). Packages in an image
closure are judged by the allowlist. Lock entries in no image — the opt-in
extras and the dev tools — are not distributed and are judged by the
Zone B denylist. `license_gate.py --zone C --python-only` runs this part
without syft; `make ci-local` runs it on every change. The code directories
and the npm lock are scanned by syft.

**Zone C is fail-closed in both directions.** A license outside the
allowlist blocks, and so does a package with *no* declared license — unless
it has a row in [License overrides](#license-overrides), which is judged by
the same allowlist. A row can never launder a license: when the publisher
gives no SPDX expression and the license text or the classifiers name a
copyleft or proprietary family (GPL, LGPL, AGPL, EPL, CDDL, EUPL, SSPL,
proprietary, non-commercial, Creative Commons), the package blocks whatever
its row says; the same holds for free text syft reports as `LicenseRef-…`.
A metadata fetch that fails, a failed `uv export`, a failed scan, a scan
that finds nothing or a lockfile that yields no package is exit 2: the gate
needs pypi.org (or a warm cache) and fails, rather than passes, without it.

## Services (Zone A)

Every compose service and Kubernetes image the swarm runs but does not build.

| service | image | license | zone | notes |
|---|---|---|---|---|
| NATS JetStream | `nats:2.12` | Apache-2.0 | A | message bus |
| Postgres + pgvector | `pgvector/pgvector:pg16` | PostgreSQL | A | source of truth, vector memory |
| Apicurio Registry | `apicurio/apicurio-registry:3.3.0` | Apache-2.0 | A | Protobuf schema registry |
| Valkey | `valkey/valkey:9.0` | BSD-3-Clause | A | locks, budgets, hand-off |
| SeaweedFS | `chrislusf/seaweedfs:latest` | Apache-2.0 | A | artifact store |
| OpenBao | `openbao/openbao:2.5.0` | MPL-2.0 | A | leased secrets |
| Ollama | `ollama/ollama:latest` | MIT | A | Tier A inference |
| vLLM | `vllm/vllm-openai:latest` | Apache-2.0 | A | `--profile vllm`, Tier B |
| LiteLLM | `ghcr.io/berriai/litellm:main-latest` | MIT | A | `--profile litellm`; core only, `enterprise/` unused |
| Qdrant | `qdrant/qdrant:latest` | Apache-2.0 | A | `--profile qdrant` |
| Forgejo | `codeberg.org/forgejo/forgejo:11` | GPL-3.0-or-later | A | `--profile forgejo`; isolated service, API over HTTP only |
| OpenSearch | `opensearchproject/opensearch:2` | Apache-2.0 | A | `--profile observability` (logs) |
| Langfuse | `langfuse/langfuse:2` | MIT | A | `--profile observability` (LLM traces); `ee/` unused |
| Jaeger | `jaegertracing/jaeger:2.22.0` | Apache-2.0 | A | `--profile observability` (traces, L3.2) |
| Prometheus | `prom/prometheus:v3.15.1` | Apache-2.0 | A | `--profile observability` (metrics, L3.2) |
| Perses | `persesdev/perses:v0.55.1` | Apache-2.0 | A | `--profile observability` (canonical dashboards, L3.2) |
| OpenObserve | `public.ecr.aws/zinclabs/openobserve:latest` | AGPL-3.0 | A | `--profile openobserve`; optional swap, unmodified, over HTTP |
| Grafana | `grafana/grafana` (operator-run) | AGPL-3.0 | A | option: import `deploy/observability/grafana-sla.json`; unmodified, over HTTP |
| Strix sandbox | `ghcr.io/usestrix/strix-sandbox:1.3.0` | Apache-2.0 (Strix) + the licenses of the security tools inside the image | A | started by the Strix CLI on the security-lane host (`make strix`, `STRIX_IMAGE`; v4.2 T8, review R10) — the tag is the default the locked strix-agent 1.7.0 declares. Pulled and run unmodified, never built or redistributed by the swarm, in no compose file |

## Built images (Zone C contents)

| image | built from | contents rule |
|---|---|---|
| swarm-agent | `deploy/Containerfile.agent` | Zone C — `uv.lock` agent + gate group closure + `agent/ ingest/ skills/ scripts/ conventions/` |
| swarm-ingest | `deploy/Containerfile.ingest` | Zone C — `uv.lock` ingest group closure + `ingest/` |
| swarm-relay | `deploy/Containerfile.relay` | Zone C — esbuild bundle of `relay/` + `relay/package-lock.json` closure |
| swarm-codex | `deploy/Containerfile.codex` | Zone C — the agent image's closure (`uv.lock` agent + gate groups) + the Codex CLI (`@openai/codex` 0.167.0, Apache-2.0, npm) and the Node.js runtime it needs (MIT), copied from `node:22-slim` (v4.2 T7.3) |
| swarm-headroom | `deploy/Containerfile.headroom` | Zone C — `uv.lock` headroom-proxy group closure (headroom-ai[proxy] 0.31.0, Apache-2.0) |

## Dependencies added by Lean & Precise v4.1.1

Direct additions only — the full closure is what the gate scans (syft over
`uv.lock`, with licenses looked up on pypi.org, and over
`relay/package-lock.json`). Licences read from the installed package
metadata unless noted.

| dependency | version | license | zone | where |
|---|---|---|---|---|
| fastembed | 0.9.1 | Apache-2.0 | C | extra `embeddings` (L2.3) |
| ↳ onnxruntime | 1.31.1 | MIT | C | fastembed runtime |
| ↳ tokenizers | (lock) | Apache-2.0 | C | fastembed runtime |
| ↳ mmh3 / loguru | (lock) | MIT | C | fastembed runtime |
| ↳ py-rust-stemmers | 0.1.8 | MIT (the wheel ships the MIT `LICENSE` file but its metadata declares none — see License overrides) | C | fastembed runtime |
| tree-sitter-language-pack | 0.13.0 | MIT OR Apache-2.0 | C | already in the agent closure via aider; used by L2.4 |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause | C | base deps, `/metrics` (L3.2) |
| ruff / pytest | (lock) | MIT / MIT | C | group `gate` — the agent image runs the quality gate with them (Phase-3 review) |
| headroom-ai[proxy] → magika / sqlite-vec / watchdog | 1.0.3 / 0.1.9 / 7.1.0 | Apache-2.0 / MIT OR Apache-2.0 / Apache-2.0 | C | group `headroom-proxy` — the three packages the `proxy` extra adds to the lock (review R1) |
| claude-agent-sdk | 0.2.180 | MIT (the SDK). **Its wheel bundles the Claude Code CLI binary, which is under Anthropic's Commercial Terms, not MIT** | B — in no image | extra `escalation` (v4.2 T7.7): the hosted-fallback lane, opt-in and off by default (INV-027). Installing the extra accepts those terms; the gate reads only the MIT metadata. The lane is the host-run worker `make escalation` (`conventions/escalation.md`): **no image closure may contain this package** — `tests/test_r11_flow.py` asserts it on the real `uv export` of every image |
| strix-agent | 1.7.0 | Apache-2.0; its closure includes `cvss` 3.6 (LGPL-3.0-or-later) and `caido-server-auth` 0.1.3 (no license metadata; MIT per the caido-community/sdk-py repository) | B — in no image | group `security` (v4.2 T8): the advisory security workflow and `make strix-scan`. Must stay out of every image — Zone C would block `cvss` |
| @openai/codex | 0.167.0 | Apache-2.0 | C | npm, installed in the Codex lane image only (v4.2 T7.3) |
| esbuild | ^0.25.12 | MIT | B | relay builder stage only — not in the runtime image (L1.4) |
| pre-commit | 4.x | MIT | B | dev group (L1.3) |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause | B | dev group — SPDX parsing in the license gate (R2); also in the agent image through pytest |
| pre-commit hooks: ruff-pre-commit, uv-pre-commit, pre-commit-hooks | v0.17.3 / 0.11.32 / v6.0.0 | MIT / MIT OR Apache-2.0 / MIT | B | developer machines only |
| uv | 0.11 (`ghcr.io/astral-sh/uv:0.11`) | MIT OR Apache-2.0 | B | builder stages only — copied out of the runtime images |
| `gcr.io/distroless/nodejs22-debian12:nonroot` | — | Apache-2.0 (distroless) + Debian package licenses | base layer | relay runtime base (L1.4) |
| `node:22-slim` | — | MIT (Node.js) + Debian package licenses | base layer | relay builder; source of the Node runtime in the Codex lane image |
| `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | — | NVIDIA's container license (proprietary; terms not checked in this session) + Ubuntu package licenses | off-swarm base layer | `deploy/antidoom/compose.yaml` only — the offline Antidoom training host (v4.2 T9). Never started by the swarm, not on its networks, not built into or redistributed with any swarm image |

Adding a service: give it a row above with its SPDX license and zone. A
service under AGPL/SSPL/BUSL stays Zone A only while it runs unmodified in
its own container and nothing in the repo imports its client library under
that license (Zone B catches that).

## License overrides

Reviewed licenses for packages **in an image** whose publisher declares
none the gate can read: no SPDX `license_expression`, a `license` field
that is not an SPDX expression, and no mapped classifier. The gate
consults a row only then, judges the license in it by the allowlist, and
ignores it when the publisher's text names a copyleft or proprietary
family. Add a row only with evidence you checked.

| package | license | evidence |
|---|---|---|
| fsspec | BSD-3-Clause | publisher declares nothing on PyPI; `LICENSE` in fsspec/filesystem_spec is the "BSD 3-Clause License" |
| multidict | Apache-2.0 | PyPI license field "Apache License 2.0", no classifier |
| protobuf | BSD-3-Clause | PyPI license field "3-Clause BSD License", no classifier |
| sqlite-vec | MIT OR Apache-2.0 | PyPI license field "MIT License, Apache License, Version 2.0"; asg017/sqlite-vec ships `LICENSE-MIT` and `LICENSE-APACHE` |
| tiktoken | MIT | PyPI license field is the MIT text ("MIT License Copyright (c) 2022 OpenAI, Shantanu Jain"), no classifier |
| transformers | Apache-2.0 | PyPI license field "Apache 2.0 License", no classifier |

Until review R2 this table had 87 rows, because the gate then judged the
whole lock through syft, which never reads classifiers. The gate now reads
the classifiers itself and judges only what ships, so the other rows are
no longer consulted; their evidence was the publisher's own classifier or
license field, which is what the gate reads directly today.

## Bundled-runtime acknowledgements

An unmodified GCC runtime library shipped inside a third-party wheel. One
row per package version and library; the gate accepts only the two licenses
below, and a new version of the package needs new rows. Evidence is the
publisher's own license text on PyPI plus the files in the installed wheel.

| package | version | bundled library | license | evidence |
|---|---|---|---|---|
| numpy | 1.26.4 | libgfortran (GCC runtime library) | GPL-3.0-or-later WITH GCC-exception-3.1 | publisher: "Name: GCC runtime library Files: numpy/.dylibs/libgfortran*, numpy/.dylibs/libgcc* Description: dynamically linked to files compiled with gcc … License: GPL-3.0-with-GCC-exception"; wheel: `numpy.libs/libgfortran-040039e1.so.5.0.0` |
| numpy | 1.26.4 | libquadmath | LGPL-2.1-or-later | publisher: "Name: libquadmath Files: numpy/.dylibs/libquadmath*.so Description: dynamically linked to files compiled with gcc … License: LGPL-2.1-or-later"; wheel: `numpy.libs/libquadmath-96973f99.so.0.0.0` |
| scipy | 1.15.3 | libgfortran (GCC runtime library) | GPL-3.0-or-later WITH GCC-exception-3.1 | publisher: "Name: GCC runtime library Files: scipy/.dylibs/libgfortran*, scipy/.dylibs/libgcc* Description: dynamically linked to files compiled with gcc … License: GPL-3.0-with-GCC-exception"; wheel: `scipy.libs/libgfortran-040039e1.so.5.0.0` |
| scipy | 1.15.3 | libquadmath | LGPL-2.1-or-later | publisher: "Name: libquadmath Files: scipy/.dylibs/libquadmath*.so Description: dynamically linked to files compiled with gcc … License: LGPL-2.1-or-later"; wheel: `scipy.libs/libquadmath-96973f99.so.0.0.0` |

Both packages are in the agent image (aider's closure). Their own code is
BSD-3-Clause; the publisher's license text also reproduces the licenses of
everything bundled in the wheel, the GPL included, which is why the gate
stops on it until these rows exist. With the standard GPL-3.0 sentences
removed, the only refused families left in either text are GPL and LGPL —
exactly what the four rows declare.

## What the Zone C gate shows

**Python — run for real** (`license_gate.py --zone C --python-only`,
2026-11-10, publisher metadata for all 373 registry packages in `uv.lock`):
exit 0.

| image | pins | SPDX `license_expression` | SPDX id in `license` | classifiers | override row |
|---|---:|---:|---:|---:|---:|
| agent | 120 | 43 | 35 | 39 | 3 |
| ingest | 69 | 33 | 20 | 13 | 3 |
| headroom | 92 | 46 | 24 | 17 | 5 |
| codex (v4.2 lane) | 120 | 43 | 35 | 39 | 3 |

- 184 lock entries are in no image (opt-in extras, the `security` group, dev
  tools, the other resolution forks) and pass the Zone B denylist. Among
  them are the 16 proprietary NVIDIA CUDA wheels the torch extras pull in,
  torch itself, pypdfium2, and from v4.2 `cvss` (LGPL-3.0-or-later, via
  strix-agent) and claude-agent-sdk. They are not distributed; an operator
  who installs an extra or the security group takes on its licenses.
  `make torch-cpu-lock` still removes the NVIDIA wheels from the lock.
- numpy 1.26.4 and scipy 1.15.3 pass on their acknowledgement rows; the gate
  prints them on every run.
- Allowlist ids that only this run made necessary: `MIT-CMU` (pillow) and
  `CNRI-Python` (regex) in the agent image, `MIT-0` (cffi 2.1.1) and `Zlib`
  (numpy 2.5.4) in the headroom image (DECISIONS D-036).

**Still deferred** (needs syft; `make verify-integration`): the syft scan of
`agent/ ingest/ relay/ skills/ scripts/` and `relay/package-lock.json` for
Zone C, and of the whole repository for Zone B. syft has not run against
this tree. The relay's npm lock carries a `license` field per package, so a
package there that syft reports without an SPDX id will need a row above.
