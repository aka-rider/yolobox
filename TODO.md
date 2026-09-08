# Upstream reports owed

Texts, diffs and the posting runbook live in `upstream/`; see `upstream/POST.md`.

- lima-vm/lima issue: port forwards stay dead once the guest agent has been unreachable for more than 10 s, a regression from PR #4889 (v2.1.2). `upstream/lima-portfwd-issue.md`.
- lima-vm/lima PR: resolve the guest agent client per dial instead of capturing it per listener. `upstream/lima-portfwd-pr.md`; the commit sits in the guest at `~/wrk/lima-vm/lima`.
- nixos-lima PR: `restartIfChanged = false` on both lima units, because a nixpkgs bump alone restarts them and that triggers the bug above. `upstream/nixos-lima-no-restart-on-switch.md`, commit in the guest at `~/wrk/nixos-lima/nixos-lima`.
- herdrdev/herdr issue: `report-agent` answers `ok` but is silently dropped, with no expiry, once a pane's own native agent of that kind has exited — the cause of at least one invisible in-VM claude session. `upstream/herdr-process-exit-latch.md`.

# Known problems

- `limactl stop` hung once on a throwaway instance whose forwards were in
  the dead state above: three minutes, then `did not receive an event
  with the "exiting" status`, host agent never exited, `limactl delete -f`
  was the only way out. The recovery this repo documents for yolobox,
  `limactl stop yolobox && ./yo up`, has worked so far; if it ever hangs,
  that is the shape. Seen once on 2026-09-05, not isolated.
- The `ok`/`fail`/`step` report vocabulary now exists three times:
  `nix/guest/yolobox-guest.sh`, `nix/checks/herd-check.sh` (which also
  carries `skip`/`inconclusive` and accumulates failure *names*), and
  Python's `Report` class in `yo`. A shared `nix/guest/report.sh` sourced
  by both guest scripts is the right eventual answer, but it would couple
  two independently-versioned derivations, which is why it did not land in
  the same change that moved `yo`'s guest-side bash into
  `nix/guest/yolobox-guest.sh`.
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
- `herdr_server_field` in `yo` is a hand-rolled stateful parser that walks
  `herdr status`'s indented text output line by line looking for a
  `server:` block. `herdr status --help` on this Mac (herdr 0.8.2) shows a
  `--json` flag, and `herdr status --json` returns a flat object with
  `server.status` and `server.compatible` among its fields — exactly the
  two `herdr_server_field` extracts today. The honest fix is `herdr status
  --json` piped through `json.loads`, which deletes the indented-block
  parser entirely rather than polishing it.
