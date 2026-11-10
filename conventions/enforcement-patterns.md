# Enforcement patterns mirrored from Claude Code (v4.2 T7.8)

Claude Code / Agent SDK contribute enforcement *patterns*, not their
Claude-only runtime. Each is translated onto machinery the swarm already
runs, so adopting them tightens existing gates rather than adding a new
dependency:

| Claude Code pattern | Swarm equivalent (already enforced) |
|---|---|
| **SubagentStop hook** — gate a subagent's result before folding it in | The **green-build gate** (T2.1) in `agent/sandbox.py`: a result that is not `ruff`+`pytest` green is never pushed. |
| **Subagent isolation** — isolated context/tools/model | The **no-shared-context verifier** (T2.3, `agent/verifier.py`): reviews only the diff + spec, never the author's conversation. |
| **Hooks (PreToolUse / PostToolUse / PostToolUseFailure)** — deterministic gating | The **CI gate chain** (`.forgejo/workflows/ci.yaml`) + the failing-tool-output-grounded repair loop. |
| **Performance Outcomes** — a rubric grader forces revision on a miss | The **rubric grader** (`grade_rubric()` in `agent/verifier.py`): a MISS forces **exactly one** revision, then escalates — never an unbounded loop. |
| **canUseTool / permission modes / checkpoints** | Allow/deny + rewind semantics on the API-fallback lane only. |

## The rubric-miss rule (bounded)

1. After a verifier pass, run `grade_rubric(bundle)`.
2. On **MISS** (score < `RUBRIC_PASS_THRESHOLD`, default 0.75): force one
   revision with the rubric notes as grounded feedback.
3. On a **second MISS**: stop revising — escalate (API-fallback lane if
   `ROUTE_API_FALLBACK=1`, else DLQ → needs-human). Doom loops are the
   failure mode T5.3/T9 guard; the rubric never becomes one.

## Verify

The rubric-miss path forces exactly one revision on a fixture, then
escalates (T7.8 acceptance).
