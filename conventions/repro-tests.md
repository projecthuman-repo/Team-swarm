# Reproduction-test-first policy (v4.1 T2.2)

When the task describes a bug or defect and no test demonstrates it:

1. FIRST commit a failing reproduction test that captures the reported
   behavior. The test must fail for the reported reason (assert on the
   symptom, not on internals).
2. THEN commit the fix that makes the reproduction test pass.
3. Keep both commits in that order on the feature branch — the reviewer
   verifies the sequence.

Never fix a bug without a test proving it existed. If the bug cannot be
reproduced, say so and fail the task with the reason instead of guessing.
