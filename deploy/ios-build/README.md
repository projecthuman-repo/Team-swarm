# iOS build container (optional — runbook Appendix A)

For React Native / iOS targets only. This is the **one component outside
the no-egress agent boundary**, because Xcode and code-signing require the
Apple toolchain, which cannot run in a Linux container. Linux targets
build in a normal Forgejo Actions container; iOS is the documented
exception.

- **Trigger:** merge to `integration`, or nightly.
- **Runner:** a macOS Forgejo Actions runner (`runs-on: macos`), on a
  separate node from the swarm.
- **Inputs:** `integration` checkout; signing secrets leased from OpenBao
  at build time (never stored on the runner).
- **Output:** `.ipa` / `.app` uploaded to SeaweedFS at
  `builds/ios/<sha>`; a build-status event written through the outbox to
  `swarm.events.build`.
- **Isolation:** no access to the agent NetworkPolicy or the swarm DB
  beyond inserting the build-status outbox row.

See `workflow.yaml` for the Actions definition.
