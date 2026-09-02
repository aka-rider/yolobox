# Operator actions

- Commit 6ea55d0 carries a leftover `# Conflicts:` block in its message. No
  release tag exists yet, so a reword via `git rebase -r 6ea55d0^` followed
  by a force-push of `main` is still possible; it rewrites every commit
  after it, so it is the operator's call.

# Upstream reports owed

- pingdotgg/t3code: the Claude capability probe hardcodes
  `strictMcpConfig: true`, which Claude Code refuses whenever an enterprise
  MCP config exists, and t3 swallows the failure, so its Claude provider
  silently ends up unauthenticated with no slash commands. Carried as a
  `sed` in `nix/pkgs/t3.nix`; the enterprise config that triggered it is
  gone from `nix/mcp.nix`, so the flip may now be droppable — proving that
  needs a paired t3 session after a rebuild, and `~/.t3/caches/claudeAgent.json`
  must show `auth.status` other than `unknown`.
- lima-vm/lima: on guest-agent restart the host agent replaces its
  `grpc.ClientConn`, but `pkg/portfwd/listener.go`'s `forwardTCP` keeps the
  dialer an existing listener captured, so every forwarded port is dead
  until the host agent restarts. Confirmed in v2.2.0 source, still present
  on master; nearest report is issue #4558. Pinned shut here with
  `restartIfChanged = false` on both lima units in `nix/base.nix`.
- nixos-lima: `lima-init` and `lima-guestagent` should carry
  `restartIfChanged = false` themselves, since their unit text rehashes on
  every nixpkgs bump and a restart triggers the lima bug above.
