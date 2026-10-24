# Upstream

Posted 2026-10-02, awaiting review. The reasoning behind it is in lima/yolobox.md#an-empty-page-means-limas-forwarder-died. The lima side is done: lima-vm/lima#5557 was closed and PR #5558 merged on 2026-10-03, and the fix shipped in lima v2.2.1.

- nixos-lima PR [nixos-lima/nixos-lima#125](https://github.com/nixos-lima/nixos-lima/pull/125): `restartIfChanged = false` on `lima-init` and `lima-guestagent`, because a nixpkgs bump alone restarts them and, on lima v2.1.2 through v2.2.0, that triggers the dead-forward bug. Still open and unmerged as of 2026-10-16. Fork `aka-rider/nixos-lima`, commit `ea30db4`; checkout in the guest at `~/wrk/nixos-lima/nixos-lima`.
  - Once it is merged and this flake's `nixos-lima` input is bumped, this box's own `restartIfChanged = false` becomes redundant: remove it.
- herdrdev/herdr#4833 (open): herdr's own selection and copy mode in machine panes copies server-side in 0.9.x, so the text never reaches the Mac clipboard. pi's OSC 52 opt-in cannot fix it, because the copy happens in the herdr server, which has no tty. Also: the Mac client drops a Clipboard message from a machine that is not the active one. Cause from source (v0.9.5): `src/client/endpoint/message_policy.rs` accepts `Clipboard` only from the active endpoint, deliberately, since 9e9bc8a14 (#3670). Issue drafted 2026-10-19, not filed; the draft is `upstream/herdr-inactive-machine-clipboard.md`.
- herdrdev/herdr: nothing owed. The root cause of the silently dropped `report-agent` is posted as a comment on [herdrdev/herdr#4755](https://github.com/herdrdev/herdr/issues/4755#issuecomment-5950076192).
- lima issue, not yet written up: `limactl stop yolobox` (and the stop half of `limactl restart`) hangs on the `vz` driver, lima 2.2.0 and 2.2.1. Four sightings, none isolated to a trigger:
  - 2026-09-05, a throwaway instance whose forwards were dead: three minutes, then `did not receive an event with the "exiting" status`; the host agent never exited and `limactl delete -f` was the only way out.
  - 2026-09-28, during the AWS broker transport spike, with no dead-forward state involved: the same message, the guest-agent grpc stream reset partway through, and a child `limactl hostagent` (parent: the real hostagent) spinning at ~98% CPU that outlived `limactl stop -f` of its parent and had to be `kill -9`'d.
  - 2026-10-01: the same again, the child spinning at ~99% and still alive after the guest rebooted from inside.
  - 2026-10-17, lima 2.2.1, `yo bootstrap`'s `limactl restart` after five days of uptime: the same message after three minutes. The hostagent took SIGINT, stopped the 1Password reverse forward, kept logging time-sync lines, and never exited. A second `limactl hostagent` spun at ~86% CPU. A later plain `limactl stop` worked.

  The gentler recovery when `limactl stop` hangs is `sudo systemctl reboot` inside the guest (`yo ssh sudo systemctl reboot`); the child still needs `kill -9`. Not written up until a narrower repro exists.
- t3code: the crash loop after `t3 update` across a service-launcher protocol bump (2026-10-01, stale `.service-stopping` marker) is pingdotgg/t3code#12627, open; its fix PR #12629 was closed unmerged on 2026-10-12. Nothing to file; `yo status` names the recovery.

# Known problems

- `limactl stop yolobox` hangs on the vz driver (lima 2.2.0 and 2.2.1); see the lima entry under Upstream.
- GitHub `environment: release` protection for HOMEBREW_TAP_TOKEN not configured.
- Tailnet ACLs for the yolobox node not reviewed.
- Immutable releases must stay off: release.yml force-moves the tag and re-uploads with --clobber.
