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

- Strix runs as its upstream CLI from the locked `security` dependency
  group (`make strix` / `make strix-scan`, one script) on a dedicated
  security-lane host — never in the sealed coding container, and in no image
  the swarm builds. The target is `STRIX_TARGET` (default: this repository);
  point it only at in-org repos.
- The CLI runs the target in its own sandbox container, `STRIX_IMAGE`,
  pinned to the tag the locked strix-agent declares. No swarm container
  mounts the host's container socket for it.
- The only inference it reaches is the self-hosted `LLM_API_BASE`
  (LiteLLM/vLLM serving Ornith); telemetry is off (`STRIX_TELEMETRY=false`).
- There is no Strix compose service or Kubernetes Job (review R10): v4.2's
  named an image upstream does not publish.
- The CI security workflow (`.forgejo/workflows/security.yaml`) only ever
  scans the repo it runs in.

Strix **augments** the always-on `bandit` static scan (which is never
removed); it does not replace it.
