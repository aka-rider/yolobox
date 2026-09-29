# Upstream reports owed

Texts, diffs and the posting runbook live in `upstream/`; see `upstream/POST.md`.

- lima-vm/lima issue: port forwards stay dead once the guest agent has been unreachable for more than 10 s, a regression from PR #4889 (v2.1.2). `upstream/lima-portfwd-issue.md`.
- lima-vm/lima PR: resolve the guest agent client per dial instead of capturing it per listener. `upstream/lima-portfwd-pr.md`; the commit sits in the guest at `~/wrk/lima-vm/lima`.
- nixos-lima PR: `restartIfChanged = false` on both lima units, because a nixpkgs bump alone restarts them and that triggers the bug above. `upstream/nixos-lima-no-restart-on-switch.md`, commit in the guest at `~/wrk/nixos-lima/nixos-lima`.
- herdrdev/herdr issue: `report-agent` answers `ok` but is silently dropped, with no expiry, once a pane's own native agent of that kind has exited — the cause of at least one invisible in-VM claude session. `upstream/herdr-process-exit-latch.md`. See "Known problems" below: this report may no longer describe a bug reachable from this box.
- lima-vm/lima issue, not yet written up: on lima 2.2.0's `vz` driver, a
  plain `limactl stop yolobox` — no dead-forward state involved, unlike
  the bullet above — hung past lima's own three-minute deadline with `did
  not receive an event with the "exiting" status`, and the guest-agent
  grpc stream reset partway through. `ps` during the hang showed a
  second, child `limactl hostagent` process (parent: the real hostagent),
  spinning at ~98% CPU, that outlived `limactl stop -f` of its parent and
  had to be `kill -9`'d by hand. Seen during the 2026-09-28 spike into the
  AWS broker's transport (see CLAUDE.md, "AWS credentials"); not isolated
  to a specific trigger yet, so not written up under `upstream/` until it
  reproduces again with a narrower repro.

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
- Published releases up to and including v1.0.1 cannot be rebuilt at all.
  `nix/base.nix` carried a stray `programs.git-lfs.enable = true`
  (introduced by `13eae86`, "enable git-lfs globally") two lines below the
  correct `programs.git.lfs.enable`, and `programs.git-lfs` is not a
  NixOS option in any nixpkgs branch — checked against nixos-unstable,
  nixos-25.11 and nixos-25.05, where `nixos/modules/programs/git-lfs.nix`
  is 404 and `module-list.nix` has no git-lfs entry. So every
  `nixos-rebuild --flake 'github:aka-rider/yolobox/v1.0.1#yolobox'` dies
  in evaluation with "The option `programs.git-lfs' does not exist",
  Mullvad or any other `/etc/yolobox/local.nix` customisation or not —
  which also means the box running 1.0.1 was never built from the
  published v1.0.1 tree as-is. `ede89be` removed the line, so v1.1.0 and
  later are clean (verified by reading both tags' trees); v1.0.0 and
  v1.0.2 were not checked and any tree between `13eae86` and `ede89be`
  carries it. Two things are owed: a release-time check that the flake
  evaluates at all before a tag is published — CI's `nix flake check`
  would have caught this if it ran against the release tree — and a
  decision whether to yank or re-point the broken tags, since a box in
  the field at that version cannot apply a single local customisation
  until its operator picks a newer ref.
- Guest egress dies completely whenever a WireGuard tunnel on the Mac
  owns the default route (seen 2026-09-16 with the `nl-ams-wg-006`
  profile in WireGuard.app). lima NATs the VM's traffic out through the
  host's default route, the tunnel does not carry those forwarded
  packets, and they are blackholed: from the guest every destination
  fails on every port — GitHub on 443 and 80, 1.1.1.1 on 53, even the
  LAN router — while `192.168.5.2:53`, lima's own resolver, keeps
  answering because the host process serves it. The Mac itself is fine
  throughout, so it looks like a guest-only fault. `nixos-rebuild` fails
  with GitHub fetch timeouts. Recognise it by that pattern and check
  `scutil --nc list` on the Mac before debugging anything in the VM;
  disconnecting the tunnel restores egress immediately. Worth a `yo`
  doctor check, and worth a note in `CLAUDE.md`.
- A narrower, opposite-looking shape from the WireGuard blackhole above:
  with ProtonVPN connected on the Mac (verified live 2026-09-28), the
  guest's general egress and even `ssh git@github.com` kept working, but
  every guest connection to `192.168.5.2:<port>` — gvproxy's NAT of
  lima's gateway address to the Mac's own loopback — was silently
  blackholed. This is exactly the path a forwarded TCP port relies on
  (not lima's own resolver on `:53`, which the WireGuard bullet above
  found still answering through a different mechanism), so it is why the
  AWS broker now reaches the guest over the same reverse unix-socket
  forward 1Password already used, rather than the TCP port it used
  before (see CLAUDE.md, "AWS credentials"). Worth a `yo` doctor check
  that tells the two shapes apart, since neither produces an error
  anywhere on its own.
- `yo status` says nothing about tailscale (see CLAUDE.md, "Tailscale: t3
  on the tailnet"). It should report whether the box is logged in
  (`tailscale status --json`'s `BackendState`) and whether
  `yolobox-tailscale-serve` is active, the same way it already reports
  the 1Password and AWS broker forwards, so a login that has expired or
  never happened is visible from the Mac without an `ssh` round trip.
- Now that t3 is reachable over the tailnet, decide whether lima's
  `hostIP: "0.0.0.0"` forward of 3773 (see CLAUDE.md, "t3: a nix-built
  npm CLI, run as a service") should be narrowed back to loopback. The
  tailnet forward makes the `0.0.0.0` LAN exposure redundant for anyone
  who has joined the tailnet; it is not redundant for a Mac-only setup
  that never runs `tailscale up`, so this is a product decision, not a
  bug.
