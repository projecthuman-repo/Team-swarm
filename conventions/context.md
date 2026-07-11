# Context-management policy (v4.1 T6.4)

Rationale: **context rot** — model reliability degrades as input grows,
even well inside a 256K window. Every mechanism below exists to keep
working context small and grounded.

| Need | Mechanism |
|---|---|
| Shrink tool/JSON traffic | Headroom compaction (proxy sidecar, `HEADROOM_ENABLED=1`) |
| Remember durable facts | structured note-taking via `lessons` (tagged; see T0.2) |
| Hand off to a sub-agent / verifier | Headroom SharedContext (`agent/shared_context.py`) — diff + spec only, never the author conversation |
| Hand off large artifacts | SeaweedFS + `spec_ref` (store the blob, pass the reference) |
| Repo knowledge | bounded retrieval pack (`agent/context_pack.py`): files + signatures under a hard cap (`CONTEXT_PACK_TOKENS`); similar-snippet retrieval is banned (degrades results up to 15%) |

aider-specific caps (set in `.env.example`):

- `AIDER_MAP_TOKENS=1024` — repo-map budget; the map is a directory, not
  a copy of the code.
- `CONTEXT_PACK_TOKENS=2000` — retrieval pack hard cap.

If a task seems to need more context than the caps allow, that is a
decomposition signal (see orchestration.md), not a reason to raise caps.
