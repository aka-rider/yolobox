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
