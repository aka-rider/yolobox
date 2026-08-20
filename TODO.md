- Commit 6ea55d0 message carries a leftover "# Conflicts:" template block; scrub with a history rewrite (e.g. `git rebase -r 6ea55d0^` reword) before any publication, then force-sync the VM clone
- Lift the opencode/crush LSP entries (basedpyright, currently per-project in
  PortHub's `opencode.json`/`.crush.json`) into the global renders in
  `nix/mcp.nix` once the parallel MCP/display work there is committed —
  shapes are schema-checked against https://opencode.ai/config.json and
  https://charm.land/crush.json (opencode: command array + extensions with
  dots; crush: command string + filetypes without dots; LSP commands stay
  bare names resolved from the project devbox PATH, unlike MCP servers).
- Report upstream (pingdotgg/t3code): the Claude capability probe hardcodes
  `strictMcpConfig: true`, which Claude Code refuses whenever an enterprise MCP
  config is present, and t3 swallows the failure so its Claude provider silently
  ends up unauthenticated with no slash commands. Affects any enterprise MCP
  deployment, not just yolobox. Carried as a `substituteInPlace` in
  `nix/pkgs/t3.nix`; drop it once upstream fixes it.
