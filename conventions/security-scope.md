# Security-scope guardrail (v4.2 T8.5)

Strix is an **offensive** autonomous pentesting agent. Per its own "only
test what you own" rule, and to keep the swarm ethical and legal:

## Allowed targets (and nothing else)

- The swarm's **own repositories** in this Forgejo org (`$FORGEJO_OWNER`).
- The swarm's **own services** on `swarm-internal` when a running target
  is required (Strix needs a live target for DAST).
- Scans run against the **`integration` branch** diff by default
  (`--scope-mode diff --diff-base origin/integration`).

## Forbidden

- Any third-party system, external host, or repository not owned by this
  org. Never point `--target` at a URL or repo you do not own.
- Production customer systems. Test environments use synthetic data only
  (Hard Rule 8), and Strix respects the same boundary.

## Enforcement

- The Strix service (`deploy/compose.yaml` profile `security`) sits on a
  dedicated lane, not the sealed coding container, and its target list is
  restricted to in-org repos by config.
- `deploy/k8s/strix.yaml` runs in the `swarm` namespace under the same
  default-deny NetworkPolicy; the only reachable inference is the
  self-hosted `LLM_API_BASE` (LiteLLM/vLLM serving Ornith).
- The CI security workflow (`.forgejo/workflows/security.yaml`) only ever
  scans the repo it runs in.

Strix **augments** the always-on `bandit` static scan (which is never
removed); it does not replace it.
