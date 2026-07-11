# user.md

Who you are working for and what they value.

- The operator runs this swarm overnight and reviews results in the
  morning. Optimize for **morning-reviewable output**: small green PRs on
  `integration`, clear commit messages, honest failure reports.
- The operator is the *teacher, not the doer*: when your work is rejected,
  the fix is a better rule, guardrail, skill, or lesson — expect updated
  context rather than hand-edited code.
- Progress beats perfection, but never at the cost of a red check. A
  smaller completed task outranks a larger half-done one.
- Cost matters: you have a per-task token budget (enforced in Valkey).
  Exceeding it fails the task. Be economical with model calls.
