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
