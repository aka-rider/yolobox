# Upstream reports owed

Texts, diffs and the posting runbook live in `upstream/`; see `upstream/POST.md`.

- lima-vm/lima issue: port forwards stay dead once the guest agent has been unreachable for more than 10 s, a regression from PR #4889 (v2.1.2). `upstream/lima-portfwd-issue.md`.
- lima-vm/lima PR: resolve the guest agent client per dial instead of capturing it per listener. `upstream/lima-portfwd-pr.md`; the commit sits in the guest at `~/wrk/lima-vm/lima`.
- nixos-lima PR: `restartIfChanged = false` on both lima units, because a nixpkgs bump alone restarts them and that triggers the bug above. `upstream/nixos-lima-no-restart-on-switch.md`, commit in the guest at `~/wrk/nixos-lima/nixos-lima`.
- herdrdev/herdr issue: `report-agent` answers `ok` but is silently dropped, with no expiry, once a pane's own native agent of that kind has exited — the cause of at least one invisible in-VM claude session. `upstream/herdr-process-exit-latch.md`. See "Known problems" below: this report may no longer describe a bug reachable from this box.

# Known problems

- `limactl stop` hung once on a throwaway instance whose forwards were in
  the dead state above: three minutes, then `did not receive an event
  with the "exiting" status`, host agent never exited, `limactl delete -f`
  was the only way out. The recovery this repo documents for yolobox,
  `limactl stop yolobox && ./yo up`, has worked so far; if it ever hangs,
  that is the shape. Seen once on 2026-09-05, not isolated.
- The `ok`/`fail`/`step` report vocabulary this bullet used to flag as
  tripled is down to one bash copy: `nix/checks/herd-check.sh`, the
  second guest-side script it named, was deleted along with the rest of
  the host-forwarded herd wiring (see CLAUDE.md, "herdr: the VM runs its
  own server, panes are real ptys"). What remains is
  `nix/guest/yolobox-guest.sh`'s bash copy and Python's `Report` class in
  `yo` — two languages, nothing left to share a sourced file between, so
  the shared-`nix/guest/report.sh` proposal this bullet used to make is
  moot; closed by deletion rather than by writing it.
- The `gc` subparser's `description` in `build_parser()` still spells
  `target, node_modules, .next` in prose, but `BUILD_DIRS` — the actual
  list — lives in `nix/guest/yolobox-guest.sh`. Moving the CLI to argparse
  (see CLAUDE.md, "`yo`'s own CLI") retired the old `USAGE` string this
  entry used to name, but the drift itself moved house rather than closing:
  nothing checks that the subparser prose still matches the array.
- The three seed pushes in `yo` (`seed_ssh`'s two `ssh_run(AGENT,
  ["sh","-c",X], stdin=...)` calls plus `seed_gitconfig`'s) could collapse
  into one `seed_push(script, payload)` helper. Six lines saved; marginal,
  so left alone when the guest-helper move went through.
- A machine pane gets neither AWS credentials nor the forwarded 1Password
  agent, whether it is opened by `herdr machine add`'s own connection or
  by anything else that is not `yo enter`. Both still ride `yo enter`'s
  own ssh connection specifically — `cmd_enter` is what sets up the AWS
  broker's `-R` forward and passes `ForwardAgent` per invocation — and a
  pane the guest herdr server spawns on its own inherits the server
  unit's environment instead, which carries neither. A persistent forward
  plus environment on the `herdr-server` unit, or a per-call `herdr
  workspace create --env`, is still owed before AWS or GitHub-over-SSH
  work is usable from a machine pane that did not come through `yo enter`.
- `~/.local/share/claude/versions/` held three installed versions (~330 MB
  each) at one point during this session, though the launcher path unit
  (`yolobox-claude-launcher`, `nix/harnesses.nix`) is supposed to prune to
  the two newest once ten minutes have passed since each install settled.
  Not yet root-caused — check whether the path unit actually fired for
  the oldest one, or whether three updates landed inside its ten-minute
  settle window back to back.
- `upstream/herdr-process-exit-latch.md` reports a bug that no longer
  affects this box. It only ever bit the old design's host-side `ssh`
  foreground process never being recognised as the agent's own exit; the
  VM now runs its own herdr server with agents as native processes on
  real ptys (see CLAUDE.md, "herdr: the VM runs its own server, panes are
  real ptys"), so the collision the report describes has no path to occur
  here any more. Withdraw it, or re-scope it as a herdr issue independent
  of yolobox, before it is ever posted (see `upstream/POST.md`).
