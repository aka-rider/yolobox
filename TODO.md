# Upstream reports owed

- lima-vm/lima: on guest-agent restart the host agent replaces its
  `grpc.ClientConn`, but `pkg/portfwd/listener.go`'s `forwardTCP` keeps the
  dialer an existing listener captured, so every forwarded port is dead
  until the host agent restarts. Confirmed in v2.2.0 source, still present
  on master as of 2026-09-05; nearest existing report is issue #4558, which
  shows the same symptom with no diagnosis. Drafted, ready to post:
  `/private/tmp/claude-501/-Users-xiii-Developer-yolobox/bed689c2-6eda-4ba5-957a-1bf6f878ef3e/scratchpad/lima-portfwd-issue.md`
  — names the mechanism (`hostagent.go`'s `processGuestAgentEvents`,
  `listener.go`'s `forwardTCP`, `client.go`'s `HandleTCPConnection`,
  `getOrCreateClient`), gives the repro from a NixOS `switch` restarting
  `lima-guestagent`, and suggests closing and re-registering listeners on
  client replacement (or resolving the dialer through the current client
  instead of capturing it once). Pinned shut here with
  `restartIfChanged = false` on both lima units in `nix/base.nix`.
- nixos-lima: `lima-init` and `lima-guestagent` should carry
  `restartIfChanged = false` themselves, and `lima-init` also needs
  `stopIfChanged = false` so its `Requires=` cannot drag the guestagent
  down, since their unit text rehashes on every nixpkgs bump and a restart
  triggers the lima bug above. Drafted, ready to post:
  `/private/tmp/claude-501/-Users-xiii-Developer-yolobox/bed689c2-6eda-4ba5-957a-1bf6f878ef3e/scratchpad/nixos-lima-restartIfChanged.md`
  — a two-hunk diff against `lima-init.nix` plus a PR description pointing
  at the lima issue above for why the restart matters.
