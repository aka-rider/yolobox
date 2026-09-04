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

# yo

- `yo link` refuses with `remote 'yolobox' already exists` when the Mac repo
  still carries a remote from a previous box, e.g.
  `ssh://lima-yolobox/home/xiii.guest/wrk/rune` after the operator/agent
  split moved projects to `/home/agent`. Recreating the VM leaves every
  linked repo in that state, so each one needs a manual `git remote remove
  yolobox` first. Least surprise would be to update a stale remote in place
  and say so on stderr, refusing only when the existing URL points somewhere
  that is not a yolobox guest path.

## `SendEnv` is silently dropped over a shared `ControlMaster`

Measured 2026-09-04 against the running box, same connection, only the
`ControlPath` differing: over `~/.lima/yolobox/ssh-agent.sock` the guest saw
none of `YOLOBOX_HERD` / `HERDR_PANE_ID` / `HERDR_SOCKET_PATH`; with
`ControlPath=none` all three arrived. The `-R` socket forward still works over
the mux, so the session looks wired: the socket appears in `/run/yolobox`, the
reporter runs, sees no `YOLOBOX_HERD`, and exits 0 without logging — invisible
in every direction.

Nothing reaches this today, because `ssh_base()` defaults to
`ControlPath=none` (`yo:305-306`) and only `guest_herd_version` passes
`mux=True`, which carries no `SendEnv`. It is a trap for the next person who
adds `SendEnv` to a multiplexed call: assert `ControlPath=none` wherever
`send_env` is used, or have `send_env` refuse a mux argv outright.
