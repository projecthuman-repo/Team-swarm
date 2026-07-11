# guardrails.md

Hard rules. These are enforced by infrastructure, but you must also never
attempt to work around them (runbook §1).

1. **No egress.** Agents have no internet access and no `.ssh`/host mounts.
2. **Outbox only.** The transactional outbox is the only publisher into
   JetStream. Never call `js.publish()` — write an outbox row instead.
3. **Atomic claims.** Task claiming is a Postgres compare-and-set. Never
   work on a task you did not claim.
4. **Branch discipline.** The bot account pushes only to `feature/*`.
   `main` and `integration` are protected.
5. **Merge discipline.** Auto-merge targets `integration` only, never `main`.
6. **Sandboxed skills.** Executable skills run inside gVisor/Kata (Tier B)
   or the podman sandbox (Tier A). Never on the host kernel.
7. **Leased secrets.** Secrets come from OpenBao with short TTLs — never
   baked into images, env files, code, commits, or logs.
8. **Synthetic data only** in every test environment.
9. **License gate.** Only Apache-2.0 / MIT / BSD / MPL-2.0 / ISC / 0BSD
   dependencies. AGPL, SSPL, BUSL/BSL, Commons Clause, and Elastic-2.0
   block the merge — the gate is fail-closed.

Escalation: if completing a task would require violating any rule above,
fail the task with the reason. A human will re-scope it.
