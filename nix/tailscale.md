# Tailscale

The code is `nix/tailscale.nix`, which puts the box on a tailnet so t3 (see [t3code](t3.md)) is reachable from outside the Mac's own LAN, and, on a box that opted in to the [kill switch](killswitch.md), selects the exit node it leaves as the box's only way out. It opens no port for t3 in the NixOS firewall. The comments in that file give the short reason for each setting. This file holds the evidence and how to recognise each failure.

## Firewall

`nixos-fw` governs `tailscale0`, and it opens exactly two things there: TCP 22, and the tunnel's UDP 41641 through `services.tailscale.openFirewall = true`. Every other port, 3773 included, is closed to the tailnet in the kernel's filter. What reaches t3 is `tailscale serve --bg --http=3773 http://127.0.0.1:3773`, run at boot by the `yolobox-tailscale-serve` oneshot. Serve is tailscaled's own reverse proxy. It terminates on the tailnet interface inside tailscaled's netstack and forwards to the loopback port, so the request never crosses `tailscale0` in the kernel's filter and needs no rule. The unit runs `tailscale serve reset` first, so it owns the whole serve config. An entry left under an earlier tailnet name, say from a login made with a hand-typed `--hostname`, is dropped instead of served forever beside the current one.

That list is only true because of `--netfilter-mode=off` in `extraSetFlags`. By default tailscaled installs its own `ts-input` chain. The live `iptables -S` showed `-A INPUT -j ts-input` before `-A INPUT -j nixos-fw`, with `-A ts-input -i tailscale0 -j ACCEPT` inside it. Every packet from the tailnet was accepted before `nixos-fw` was consulted, so every port the agent bound on `0.0.0.0` was reachable from any tailnet peer, and "`nixos-fw` governs `tailscale0`" was false. With netfilter mode off, tailscaled adds no rules and `nixos-fw` governs `tailscale0` like any other interface. `tailscale serve` is unaffected. The tempting alternative, `iptables -I INPUT 1`, is wrong because tailscaled re-inserts its jump at the top of `INPUT` whenever it restarts. Netfilter mode off also drops tailscaled's anti-spoofing rule for 100.64/10, which the `ts-input` chain used to carry.

Recognise a regression with `sudo iptables -S INPUT | head -3`, which must show no `ts-input` jump.

## sshd on two interfaces

`nix/base.nix` sets `services.openssh.openFirewall = false`, so NixOS opens no port for sshd by default. Port 22 is opened per interface on exactly two: `tailscale0` (in `nix/tailscale.nix`, by key only, see [Two accounts](../docs/accounts.md#two-accounts)) and `enp0s1` (in `nix/base.nix`). `enp0s1` is lima's NAT network, 192.168.5.15. The gateway 192.168.5.2 is the Mac, and nothing on the LAN can reach that subnet. The per-interface list is declarative and works on the iptables and nftables backends alike. An earlier approach, an `ip46tables ... -j DROP` in `extraCommands` plus an assertion against nftables, did not.

The `enp0s1` rule exists because lima's hostagent can reach the guest's sshd over TCP on that address. Its readiness wait polls 192.168.5.15:22 before `limactl start` reports the instance up, and its usernet forwarder dials the same address. Under limactl 2.2.1 the hostagent takes one of two paths at start.

1. When it detects sshd on the vsock port, it logs `Started vsock forwarder: 127.0.0.1:60022 -> vsock:22 on VM` and `Detected SSH server is listening on the vsock port; changed 127.0.0.1:60022 to proxy for the vsock port`. It never waits on TCP. The 2026-10-24 boot did this 17 s after start.
2. Otherwise it waits up to 600 s on TCP 192.168.5.15:22, then falls back to its usernet forwarder, which dials the same address.

On 2026-10-22 the log carried no vsock line at all, and setting `ssh.overVsock: true` on the instance (`limactl edit yolobox --set '.ssh.overVsock = true'`) did not change that. Why detection missed that day is not known. With 22 open on `enp0s1` the TCP path succeeded and the box came up. So the TCP fallback is real, and `enp0s1` must allow 22 whatever `ssh.overVsock` says. The earlier belief that `enp0s1` needs nothing was wrong.

The rule was added for the first boot under limactl 2.2.1 after `2e7cb35`, which had opened 22 on `tailscale0` only. That boot died in `yo up` after 600 s with `timed out waiting for port to become available on 192.168.5.15:22`, then `did not receive an event with the running status`. `~/.lima/yolobox/ha.stderr.log` showed the wait starting at 16:02:43 and timing out at 16:12:37, then `Failed to wait for the guest SSH server to become available, falling back to usernet forwarder`. Every dial to 192.168.5.15:22 ended in `context deadline exceeded`, not `connection refused`, because `nixos-fw` dropped the packets. `serialv.log` ended at `yolobox login:`, so the guest itself was healthy. Recognise the shape by timeouts rather than refusals, with the guest at its login prompt. The `Can't proxy a datagram to udp ... 127.0.0.1:5351` lines in the same output are tailscaled noise.

The box was recovered over the tailnet, where 22 stayed open (`ssh -i ~/.lima/_config/user <operator>@<tailnet IP>`). The working tree was tarred in and switched with `nixos-rebuild switch --impure --flake path:...#yolobox`. A box with no tailnet login would have had no way in, since root and agent are locked and the serial console is output-only.

tailscaled's own ssh server, which `tailscale up --ssh` would start, authorises by tailnet ACL instead of by this repo's keys. `extraSetFlags` therefore carries `--ssh=false`.

Checks in the VM:

- `sudo iptables -S nixos-fw | grep 'dport 22'` must show exactly two accepts, one with `-i enp0s1` and one with `-i tailscale0`.
- `ss -a --vsock | grep ':22'` shows `v_str LISTEN *:22` whenever ssh rides vsock. The operator's sessions then appear as `ESTAB 3:22 <-> 2:*` (CID 2 is the host), and `ss -tan` shows no established connection on port 22 during a `yo ssh`. The Mac's `127.0.0.1:60022` (`ssh.localPort`) is the hostagent's listener on the Mac side.

Checks from a tailnet device: `ssh <user>@yolobox.<tailnet>.ts.net` must reach sshd and offer keys, and a password prompt is a bug. `curl http://yolobox.<tailnet>.ts.net:3773` must work. The tailnet ACLs for the node are not reviewed (see `TODO.md`).

## tailscaled-set

A changed `extraSetFlags` has to reach the running box on `switch`, and for a while it did not. nixpkgs declares `tailscaled-set`, the unit that runs `tailscale set <flags>`, as a bare `Type=oneshot`. It is `inactive` once it has exited, and `switch-to-configuration` restarts a changed unit only if it is active. So the switch that added `--netfilter-mode=off` listed no `tailscaled-set` among its restarts and left the box exposed until the next boot. `systemctl is-active tailscaled-set` printed `inactive`, `tailscale debug prefs` still showed `"NetfilterMode": 2`, and `iptables -S INPUT` still had `-A INPUT -j ts-input` ahead of `nixos-fw`.

`nix/tailscale.nix` now sets `RemainAfterExit = true` on it, so the unit stays `active (exited)` and a changed flag list restarts it. The switch that first introduces `RemainAfterExit` cannot restart a unit that is still inactive. On a box that predates it, run `yo ssh sudo systemctl restart tailscaled-set` once. `systemctl show tailscaled-set -p RemainAfterExit -p ActiveState` must then answer `yes` and `active`.

## DNS and logging

`--accept-dns=false` stops MagicDNS from taking over `/etc/resolv.conf`. At tailscale's default the guest's DNS would answer through the tailnet's resolver for every name, not only `*.ts.net`. That would silently change what every other process in the box resolves, including the Mac's own DNS filter that the AWS setup relies on staying in place (see [The DNS sinkhole](../docs/mac-network.md#the-dns-sinkhole)). The box's own name is still reachable as `yolobox.<tailnet>.ts.net`, because MagicDNS runs on the client end of a tailnet connection, never in this guest's resolver.

On a box that opted in to the [kill switch](killswitch.md) the guest's resolver is `networking.nameservers`, set per box (see [DNS](killswitch.md#dns)), and there is a second reason for the flag: with MagicDNS enabled, tailscaled would forward the queries it cannot answer to upstream resolvers from its own sockets, and those carry the `0x80000` mark the kill switch lets out of the Lima interface. DNS would then leave the box directly, which is exactly what the kill switch exists to prevent.

`disableUpstreamLogging = true` sets `TS_NO_LOGS_NO_SUPPORT=true` on tailscaled. The node uploads no logs to log.tailscale.com, and for the same reason Tailscale support will not help with this node. It is working when `yo ssh journalctl -u tailscaled -b` shows `You have disabled logging`. It takes effect on the next switch, because the variable sits in the unit's environment and a changed unit makes the switch restart tailscaled.

## Login

Login is interactive and happens once, by the operator, because no auth key exists anywhere in this repo: `yo ssh sudo tailscale up`. It takes no `--hostname`. `extraSetFlags` pins the tailnet name to `networking.hostName`, so every box joins as `yolobox`, and a hand-typed `--hostname` is overwritten by the next `tailscaled-set` run.

`--netfilter-mode=off`, `--accept-dns=false` and `--hostname` are all non-default, so a bare `tailscale up` on an already logged-in node may refuse with "changing settings via 'tailscale up' requires mentioning all non-default flags". Do not answer that with `tailscale up --reset`, which clears them. Run `yo ssh sudo tailscale up`, then `yo ssh sudo systemctl restart tailscaled-set`, so the unit re-applies the flags.

State from the login, the node key and the tailnet identity, persists under `/var/lib/tailscale`. Like everything else under [Two accounts](../docs/accounts.md#two-accounts), it is VM-local. Recreating the VM loses it, and the new box logs in again the same way.

## Exit node

`yolobox.network.exitNode` (declared in `nix/killswitch.nix`) names the tailnet node all egress goes through. It is null by default, and the box then has no exit-node unit and none of the kill switch. When it is set, `yolobox-tailscale-exit-node` runs `tailscale set --exit-node=<node> --exit-node-allow-lan-access` after tailscaled is `Running`. It uses the same wait and failure messages as the serve unit, from the shared `waitForRunning` script and the shared oneshot settings in `tailscale.nix`. The kill switch is what makes the choice binding, and why the flag is needed is in [The exit node](killswitch.md#the-exit-node).

## Serve readiness

A box that has never logged in, or whose login has expired, makes `yolobox-tailscale-serve` fail instead of quietly serving nothing or retrying forever. The unit runs `tailscale wait --timeout=60s`, which blocks until the backend is `Running` with a Tailscale IP, and only then serves (`TimeoutStartSec=90s`). The comments in the script give the remedy per state.

The wait exists because tailscaled counts as "started" before it has loaded its state. On the first boot of the 2026-10-01 generation the oneshot ran 13 ms after start and saw `BackendState` `NoState` on a node that was in fact logged in. It failed and left `systemctl is-system-running` at `degraded` until someone restarted it by hand. A node that is not logged in never reaches `Running`, so `wait` times out after 60 s and the unit fails with "tailscale did not reach Running within 60s (state: X)". X is the `BackendState` the script reads (`tailscale status --json | jq -r .BackendState`, or `unreadable` when that fails) for that message only. It is never the readiness test, and there is no sleep loop chasing a state that a `tailscale up` run once was always going to settle.

`yo status` shows the same pair, `BackendState` and the serve unit's state, on its `tailscale:` line, so an expired login is visible from the Mac. t3 itself never depends on any of this. It keeps listening on loopback whether or not the tailnet is up, so t3 reached from inside the VM or through lima's Mac-side forward is unaffected by a login that has not happened yet.

## Box-local routes

The first switch into a generation with this module restarts dhcpcd, because nixpkgs' tailscale module adds `tailscale0` to `networking.dhcpcd.denyInterfaces`, which changes dhcpcd's config. On stop dhcpcd removes the lease address, and the kernel flushes every route that hung off it. That includes any static route a `/etc/yolobox/local.nix` declared through `networking.interfaces.<if>.ipv4.routes`, because its `network-addresses-<if>` unit runs once and never re-adds it. The switch reports success and the route is simply gone. Any later dhcpcd restart, such as a nixpkgs bump, does the same. A box-local route therefore belongs in `networking.dhcpcd.runHook`, re-added with `ip route replace` on every `BOUND`, `REBOOT`, `RENEW` and `REBIND`, so its lifetime follows the lease it depends on.

## Why 3773 also stays on 0.0.0.0

The tailnet path does not make lima's `hostIP: "0.0.0.0"` forward of 3773 redundant. A Mac-only setup that never runs `tailscale up` still reaches t3 from other devices on the LAN through `yo pair`'s default `http://<LocalHostName>.local:3773`. Narrowing the forward to loopback would break exactly that.
