# Upstream

Posted 2026-10-02, all awaiting review. The reasoning behind them is in CLAUDE.md, "An empty page means lima's forwarder died, not t3".

- lima issue [lima-vm/lima#5557](https://github.com/lima-vm/lima/issues/5557): port forwards stay dead once the guest agent has been unreachable for more than 10 s, a regression from PR #4889 (v2.1.2).
- lima PR [lima-vm/lima#5558](https://github.com/lima-vm/lima/pull/5558): dial through the current guest agent client. Fork `aka-rider/lima`, branch `portfwd-current-client`, commit `3be86a5` on master `348603eb`; checkout in the guest at `~/wrk/lima-vm/lima`. Its tests need cgo: `nix shell nixpkgs#go nixpkgs#gcc`.
- nixos-lima PR [nixos-lima/nixos-lima#125](https://github.com/nixos-lima/nixos-lima/pull/125): `restartIfChanged = false` on `lima-init` and `lima-guestagent`, because a nixpkgs bump alone restarts them and that triggers the bug above. Fork `aka-rider/nixos-lima`, commit `ea30db4`; checkout in the guest at `~/wrk/nixos-lima/nixos-lima`.
  - Once it is merged and this flake's `nixos-lima` input is bumped, this box's own `restartIfChanged = false` becomes redundant: remove it.
- herdrdev/herdr: nothing owed. The root cause of the silently dropped `report-agent` is posted as a comment on [herdrdev/herdr#4755](https://github.com/herdrdev/herdr/issues/4755#issuecomment-5950076192).
- lima issue, not yet written up: `limactl stop yolobox` hangs on lima 2.2.0's `vz` driver. Three sightings, none isolated to a trigger:
  - 2026-09-05, a throwaway instance whose forwards were dead: three minutes, then `did not receive an event with the "exiting" status`; the host agent never exited and `limactl delete -f` was the only way out.
  - 2026-09-28, during the AWS broker transport spike, with no dead-forward state involved: the same message, the guest-agent grpc stream reset partway through, and a child `limactl hostagent` (parent: the real hostagent) spinning at ~98% CPU that outlived `limactl stop -f` of its parent and had to be `kill -9`'d.
  - 2026-10-01: the same again, the child spinning at ~99% and still alive after the guest rebooted from inside.

  The gentler recovery when `limactl stop` hangs is `sudo systemctl reboot` inside the guest (`yo ssh sudo systemctl reboot`); the child still needs `kill -9`. Not written up until a narrower repro exists.

# Known problems

- `t3 update -y` (0.0.42 -> 0.0.44, 2026-10-01) reported "Background service restarted" and `active`, then t3code.service crash-looped on `[service-launcher] Service state is invalid or unsupported.` until systemd's start limit hit, leaving it failed and t3 down for about 80 minutes. A stale `~/.t3/runtime/.service-stopping` marker survived the update; `t3 service restart` did not clear it, `systemctl --user reset-failed t3code.service && t3 service install` did. It is a vendor bug, and `yo status` now shows it in its `t3:` line, so it is visible from the Mac, but only the recovery above fixes it.
- Two VPN shapes blackhole guest traffic differently (CLAUDE.md, "Mac VPN shapes that blackhole guest traffic"), and neither produces an error anywhere on its own. `yo` should carry a doctor check that tells them apart: all guest egress dead except `192.168.5.2:53` (a WireGuard default route) against only `192.168.5.2:<port>` dead (ProtonVPN).
