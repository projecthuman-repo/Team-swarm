# Licensing — the three-zone model (Lean & Precise L3.1)

The swarm separates **what it talks to**, **what its code links**, and
**what it ships**. Each zone has its own rule; the license gate
(`.forgejo/scripts/license_gate.py`) enforces B and C on every PR and is
fail-closed for anything that ships.

| zone | what | rule | gate |
|---|---|---|---|
| **A** | network services the swarm calls over a socket, each in its own container (no linking, no redistribution of modified code) | any OSI-approved license | `--zone A` prints the table below, exits 0 |
| **B** | everything linked or imported by swarm code anywhere in the repo (runtime, dev and CI tooling, every lockfile) | no AGPL, SSPL, BUSL/Business Source, Commons Clause | `--zone B` (default path `dir:.`) — denylist |
| **C** | the contents of the images the swarm builds and the code it ships: `agent/ ingest/ relay/ skills/ scripts/`, the Python closure (`pyproject.toml`, `uv.lock`) and the relay's npm closure (`relay/package-lock.json`) | permissive only: MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, ISC, 0BSD (+ PostgreSQL, PSF, Unlicense, CC0, BlueOak equivalents); anything else blocks | `--zone C` (default) — fail-closed allowlist |

**Image contents are Zone C.** Every image built from `deploy/Containerfile.*`
(agent, ingest, relay, headroom) ships only Zone C application code and
dependencies. The OS base layers they start from (`python:3.12-slim`,
`node:22-slim` builder, `gcr.io/distroless/nodejs22-debian12`) are
distribution aggregates under their own package licenses (glibc LGPL,
bash/coreutils GPL in Debian slim); they are platform, not swarm code, and
no swarm code links against a GPL library from them.

**Zone C is fail-closed in both directions.** A license outside the
allowlist blocks, and so does a package for which the scan finds *no*
SPDX license — none at all, or only free text that syft reports as
`LicenseRef-…` — unless it is the swarm's own code or has a row in
[License overrides](#license-overrides) below. The gate reads both SPDX
fields syft writes (`licenseDeclared` and `licenseConcluded`), requires
every package in `uv.lock` to appear in the scan, and fails when a scan
finds nothing or a lockfile yields no package. `uv.lock` carries no license data, so the gate runs syft
with `SYFT_PYTHON_SEARCH_REMOTE_LICENSES=true` (syft asks pypi.org):
Zone C needs pypi.org reachable and fails, rather than passes, without it.

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

## Built images (Zone C contents)

| image | built from | contents rule |
|---|---|---|
| swarm-agent | `deploy/Containerfile.agent` | Zone C — `uv.lock` agent + gate group closure + `agent/ ingest/ skills/ scripts/ conventions/` |
| swarm-ingest | `deploy/Containerfile.ingest` | Zone C — `uv.lock` ingest group closure + `ingest/` |
| swarm-relay | `deploy/Containerfile.relay` | Zone C — esbuild bundle of `relay/` + `relay/package-lock.json` closure |
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
| esbuild | ^0.25.12 | MIT | B | relay builder stage only — not in the runtime image (L1.4) |
| pre-commit | 4.x | MIT | B | dev group (L1.3) |
| pre-commit hooks: ruff-pre-commit, uv-pre-commit, pre-commit-hooks | v0.17.3 / 0.11.32 / v6.0.0 | MIT / MIT OR Apache-2.0 / MIT | B | developer machines only |
| uv | 0.11 (`ghcr.io/astral-sh/uv:0.11`) | MIT OR Apache-2.0 | B | builder stages only — copied out of the runtime images |
| `gcr.io/distroless/nodejs22-debian12:nonroot` | — | Apache-2.0 (distroless) + Debian package licenses | base layer | relay runtime base (L1.4) |
| `node:22-slim` | — | MIT (Node.js) + Debian package licenses | base layer | relay builder only |

Adding a service: give it a row above with its SPDX license and zone. A
service under AGPL/SSPL/BUSL stays Zone A only while it runs unmodified in
its own container and nothing in the repo imports its client library under
that license (Zone B catches that).

## License overrides

Reviewed licenses for packages whose published metadata gives the gate no
SPDX id. The gate uses a row **only** when the scan finds no license for
that package, or only free text that syft reports as `LicenseRef-…`
("MIT License", "BSD", "Apache 2.0" — syft reads PyPI's `license` and
`license_expression` fields and never the classifiers). A row never
replaces a real SPDX id, never clears text that names a denied family, and
the license in the row is judged by the same allowlist. Add a row only
with evidence you checked.

The first seven rows were checked against the upstream license file. The
rest were derived on 2026-11-10 from what each publisher states on PyPI
(the `license` field and the trove classifier, quoted as evidence) for
every package in `uv.lock` that needs a row; where the publisher says
only "BSD", the installed license text decided between the two- and
three-clause form.

| package | license | evidence |
|---|---|---|
| py-rust-stemmers | MIT | `LICENSE` file inside the 0.1.8 wheel ("MIT License, Copyright (c) 2024 qdrant") |
| fsspec | BSD-3-Clause | `LICENSE` in fsspec/filesystem_spec ("BSD 3-Clause License") |
| crewai | MIT | `LICENSE` in crewAIInc/crewAI (MIT text) |
| crewai-cli | MIT | same repository as crewai |
| crewai-core | MIT | same repository as crewai |
| cel-python | Apache-2.0 | `LICENSE` in cloud-custodian/cel-python |
| routellm | Apache-2.0 | `LICENSE` in lm-sys/RouteLLM |
| aider-chat | Apache-2.0 | classifier "Apache Software License" |
| aiosignal | Apache-2.0 | PyPI license field "Apache 2.0"; classifier "Apache Software License" |
| aiosqlite | MIT | classifier "MIT License" |
| annotated-types | MIT | classifier "MIT License" |
| ast-grep-cli | MIT | classifier "MIT License" |
| asttokens | Apache-2.0 | PyPI license field "Apache 2.0" |
| beautifulsoup4 | MIT | PyPI license field "MIT License"; classifier "MIT License" |
| blinker | MIT | classifier "MIT License" |
| chromadb | Apache-2.0 | classifier "Apache Software License" |
| colorama | BSD-2-Clause OR BSD-3-Clause | classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| confluent-kafka | Apache-2.0 | classifier "Apache Software License" |
| datasets | Apache-2.0 | PyPI license field "Apache 2.0"; classifier "Apache Software License" |
| deprecation | Apache-2.0 | PyPI license field "Apache 2"; classifier "Apache Software License" |
| diff-match-patch | Apache-2.0 | classifier "Apache Software License" |
| diskcache | Apache-2.0 | PyPI license field "Apache 2.0"; classifier "Apache Software License" |
| distro | Apache-2.0 | PyPI license field "Apache License, Version 2.0"; classifier "Apache Software License" |
| fastuuid | BSD-3-Clause | classifier "BSD License"; installed license text is the three-clause form |
| flatbuffers | Apache-2.0 | PyPI license field "Apache 2.0"; classifier "Apache Software License" |
| gitdb | BSD-3-Clause | PyPI license field "BSD License"; classifier "BSD License"; installed license text is the three-clause form |
| google-re2 | BSD-2-Clause OR BSD-3-Clause | classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| grep-ast | Apache-2.0 | classifier "Apache Software License" |
| hf-xet | Apache-2.0 | classifier "Apache Software License" |
| huggingface-hub | Apache-2.0 | PyPI license field "Apache"; classifier "Apache Software License" |
| hyperframe | MIT | PyPI license field "The MIT License (MIT) Copyright (c) 2014 …"; classifier "MIT License" |
| importlib-metadata | Apache-2.0 | classifier "Apache Software License" |
| importlib-resources | Apache-2.0 | classifier "Apache Software License" |
| ipython-pygments-lexers | BSD-2-Clause OR BSD-3-Clause | classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| jinja2 | BSD-3-Clause | classifier "BSD License"; installed license text is the three-clause form |
| jiter | MIT | classifier "MIT License" |
| json5 | Apache-2.0 | PyPI license field "Files: Everything except for the benchmar…"; classifier "Apache Software License" |
| jsonpointer | BSD-3-Clause | PyPI license field "Modified BSD License"; classifier "BSD License" |
| kubernetes | Apache-2.0 | PyPI license field "Apache License Version 2.0"; classifier "Apache Software License" |
| lancedb | Apache-2.0 | classifier "Apache Software License" |
| loguru | MIT | classifier "MIT License" |
| markdown-it-py | MIT | classifier "MIT License" |
| mccabe | MIT | PyPI license field "Expat license"; classifier "MIT License" |
| mdit-py-plugins | MIT | classifier "MIT License" |
| mdurl | MIT | classifier "MIT License" |
| mmh3 | MIT | PyPI license field "MIT License Copyright (c) 2011-2026 Hajim…"; classifier "MIT License" |
| mpmath | BSD-2-Clause OR BSD-3-Clause | PyPI license field "BSD"; classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| mslex | Apache-2.0 | PyPI license field "Apache Software License 2.0"; classifier "Apache Software License" |
| multidict | Apache-2.0 | PyPI license field "Apache License 2.0" |
| networkx | BSD-3-Clause | classifier "BSD License"; installed license text is the three-clause form |
| nodeenv | BSD-2-Clause | PyPI license field "BSD"; classifier "BSD License"; installed license text is the two-clause form |
| numpy | BSD-3-Clause | PyPI license field "Copyright (c) 2005-2023, NumPy Developers…"; classifier "BSD License"; installed license text is the three-clause form |
| nvidia-ml-py | BSD-2-Clause OR BSD-3-Clause | PyPI license field "BSD"; classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| onnxruntime | MIT | PyPI license field "MIT License"; classifier "MIT License" |
| oslex | MIT | classifier "MIT License" |
| overrides | Apache-2.0 | PyPI license field "Apache License, Version 2.0" |
| pandas | BSD-3-Clause | PyPI license field "BSD 3-Clause License Copyright (c) 2008-2…"; classifier "BSD License" |
| pathspec | MPL-2.0 | classifier "Mozilla Public License 2.0 (MPL 2.0)" |
| pdfplumber | MIT | classifier "MIT License" |
| pendulum | MIT | PyPI license field "MIT License"; classifier "MIT License" |
| pexpect | ISC | PyPI license field "ISC license"; classifier "ISC License (ISCL)" |
| prompt-toolkit | BSD-3-Clause | classifier "BSD License"; installed license text is the three-clause form |
| protobuf | BSD-3-Clause | PyPI license field "3-Clause BSD License" |
| ptyprocess | ISC | classifier "ISC License (ISCL)" |
| pyperclip | BSD-3-Clause | PyPI license field "BSD"; classifier "BSD License"; installed license text is the three-clause form |
| pypika | Apache-2.0 | PyPI license field "Apache License Version 2.0"; classifier "Apache Software License" |
| python-dateutil | Apache-2.0 OR BSD-3-Clause | PyPI license field "Dual License"; classifier "Apache Software License" + "BSD License" |
| pyvis | BSD-2-Clause OR BSD-3-Clause | PyPI license field "BSD"; both BSD variants are on the allowlist; which one was not checked |
| pywin32 | PSF-2.0 | PyPI license field "PSF"; classifier "Python Software Foundation License" |
| requests-toolbelt | Apache-2.0 | PyPI license field "Apache 2.0"; classifier "Apache Software License" |
| s3transfer | Apache-2.0 | PyPI license field "Apache License 2.0"; classifier "Apache Software License" |
| safetensors | Apache-2.0 | classifier "Apache Software License" |
| scipy | BSD-3-Clause | PyPI license field "Copyright (c) 2001-2002 Enthought, Inc. 2…"; classifier "BSD License"; installed license text is the three-clause form |
| shellingham | ISC | PyPI license field "ISC License"; classifier "ISC License (ISCL)" |
| socksio | MIT | classifier "MIT License" |
| soundfile | BSD-3-Clause | PyPI license field "BSD 3-Clause License"; classifier "BSD License" |
| sympy | BSD-2-Clause OR BSD-3-Clause | PyPI license field "BSD"; classifier "BSD License"; both BSD variants are on the allowlist; which one was not checked |
| tiktoken | MIT | PyPI license field "MIT License Copyright (c) 2022 OpenAI, Sh…" |
| tokenizers | Apache-2.0 | classifier "Apache Software License" |
| tomli | MIT | classifier "MIT License" |
| tomli-w | MIT | classifier "MIT License" |
| traitlets | BSD-3-Clause | PyPI license field "BSD 3-Clause License - Copyright (c) 2001…"; classifier "BSD License" |
| transformers | Apache-2.0 | PyPI license field "Apache 2.0 License" |
| tree-sitter | MIT | classifier "MIT License" |
| uvloop | MIT OR Apache-2.0 | PyPI license field "MIT License"; classifier "Apache Software License" + "MIT License" |
| valkey | MIT | PyPI license field "MIT License Copyright (c) 2022-2023, Redi…"; classifier "MIT License" |
| win32-setctime | MIT | PyPI license field "MIT license"; classifier "MIT License" |

## What the first real Zone C run will show

syft could not run in the session that wrote this gate. Instead, the PyPI
metadata of all 352 registry packages in `uv.lock` was read on 2026-11-10
and pushed through the rule syft applies (read from its source:
`license_expression`, else `license`, never the classifiers; text that is
not an SPDX id becomes `LicenseRef-…`) and then through the gate itself.
syft was not executed, so the real run — `make verify-integration` — can
still differ in detail.

What PyPI gives the gate, per lock entry:

| PyPI metadata | entries | gate result |
|---|---:|---|
| SPDX expression (`license_expression`) | 143 | judged: 135 pass, 8 block (below) |
| SPDX id in the `license` field | 96 | judged: all pass |
| free text in the `license` field ("MIT License", "BSD", …) | 53 | needs a row |
| classifier only (syft does not read it) | 39 | needs a row |
| nothing | 21 | needs a row |

Without the override table 121 entries would block (46 of the 120 pins in
`requirements.txt`). With its 87 rows, **23 entries block**, and these are
decisions for the owner, not gaps in the table:

- **6 declare an SPDX expression outside the allowlist** (permissive, but
  not on the list in Hard Rule 9):

  | package | declared | where |
  |---|---|---|
  | pillow 12.1.1 / 13.0.0 | MIT-CMU | agent image (aider) |
  | regex 2026.1.15 | Apache-2.0 AND CNRI-Python | agent and ingest images |
  | cffi 2.1.1 | MIT-0 | lock (agent image pins 2.0.0, MIT) |
  | numpy 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | lock (agent image pins 1.26.4) |
  | torch 2.15.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND ISC AND MIT AND Zlib | extras `embeddings-torch`, `routing` only |

- **16 NVIDIA CUDA wheels — proprietary, blocked, and they should be**
  (`nvidia-cublas`, `nvidia-cudnn-cu13`, `nvidia-nvjitlink`, `cuda-toolkit`,
  …; "NVIDIA Proprietary Software" or no license). The torch extras pull
  them into `uv.lock`; no image installs them. `make torch-cpu-lock`
  (deferred, needs download.pytorch.org) replaces them with the CPU-only
  torch build. That build is versioned `<version>+cpu`, which pypi.org
  does not list, so torch itself will then show "no license found" and
  stay blocked until the owner decides on its license set.
- **pypdfium2** (extra `orchestration`) declares "BSD-3-Clause,
  Apache-2.0, dependency licenses" — it bundles PDFium, so no single row
  was written for it.

So the two images are blocked by two packages: pillow and regex.
