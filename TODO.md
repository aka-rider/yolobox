- Commit 6ea55d0 message carries a leftover "# Conflicts:" template block; scrub with a history rewrite (e.g. `git rebase -r 6ea55d0^` reword) before any publication, then force-sync the VM clone
- Lift the opencode LSP entries (basedpyright, currently per-project in
  PortHub's `opencode.json`) into the global render in `nix/mcp.nix` once the
  parallel MCP/display work there is committed — the shape is schema-checked
  against https://opencode.ai/config.json (command array + extensions with
  dots; LSP commands stay bare names resolved from the project devbox PATH,
  unlike MCP servers).
- crush was dropped from the box (`nix/harnesses.nix`, plus its MCP render in
  `nix/mcp.nix`) because nixpkgs builds it with `doCheck = true` and its
  `TestE2E_PermissionFlowCrossClient` e2e test fails in the nix sandbox —
  the suite wants network (`catwalk.charm.land`) and the test times out. It
  gated the whole system closure. Re-add it if upstream fixes the test or
  nixpkgs disables the check; it never had herdr integration anyway.
- Report upstream (pingdotgg/t3code): the Claude capability probe hardcodes
  `strictMcpConfig: true`, which Claude Code refuses whenever an enterprise MCP
  config is present, and t3 swallows the failure so its Claude provider silently
  ends up unauthenticated with no slash commands. Affects any enterprise MCP
  deployment, not just yolobox. Carried as a `substituteInPlace` in
  `nix/pkgs/t3.nix`; drop it once upstream fixes it.
- Report upstream (lima-vm/lima): on guest-agent restart the host agent
  replaces its grpc.ClientConn but `pkg/portfwd/listener.go`'s `forwardTCP`
  never refreshes the dialer captured by an existing listener, so all
  dynamically forwarded ports are dead until the host agent restarts. Confirmed
  in v2.2.0 source, still present on master. Nearest existing report is issue
  #4558 (open, no root cause assigned). Workaround is `restartIfChanged = false`
  on the guest-agent units to prevent `nixos-rebuild switch` from triggering
  the restart.
- Report to nixos-lima: `lima-init` and `lima-guestagent` units should carry
  `restartIfChanged = false`, since restarting the guest agent breaks the host's
  port forwarding and their unit text churns textually (store-path rehashing)
  on every nixpkgs bump even when semantics are identical. The consequence is
  that a `nixos-rebuild switch` with a nixpkgs version bump silently breaks all
  dynamic port forwarding until a host-agent restart.
- rune/Cargo.toml declares only `[profile.dist]` and no `[profile.dev]`, so dev
  builds carry full unstripped DWARF and each integration test links its own
  ~230M binary; cargo never prunes old hashes. This caused 41G of bloat in a
  five-day run. The fix is a `[profile.dev]` with `debug = "line-tables-only"`
  plus `[profile.dev.package."*"] debug = false` in the rune repo. Without it,
  `yo gc --deep` becomes a recurring chore rather than a one-off cleanup.
