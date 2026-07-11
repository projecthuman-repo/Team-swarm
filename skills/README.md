# skills/

Executable tools agents may invoke — always through the sandbox
(Hard Rule 6), never on the host kernel.

A skill is a directory containing an executable named `run` (any
language). Input arrives on stdin, results go to stdout.

```bash
echo world | skills/run_skill.sh hello          # -> hello, world
skills/run_skill.sh hello --prove-isolation     # sandbox self-test
```

Sandbox guarantees (Tier A, `run_skill.sh`): no network, read-only rootfs,
64MB tmpfs workdir, all capabilities dropped, seccomp allowlist, memory/
cpu/pid limits. Tier B runs skills as gVisor pods
(`deploy/k8s/skill-runner.yaml`); swap `runtimeClassName` to `kata` for a
dedicated-kernel microVM.
