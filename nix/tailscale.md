# Tailscale

The code is `nix/tailscale.nix`, which puts the box on a tailnet so that its services are reachable from outside the host's own LAN. Tailscale is optional: a box that never runs `tailscale up` works as before, and nothing in boot waits for a login. The comments in that file give the short reason for each setting. This file holds the evidence and how to recognise each failure.

## Firewall

`tailscale0` is a trusted interface (`networking.firewall.trustedInterfaces = [ "tailscale0" ]`), so `nixos-fw` accepts everything that arrives on it. The tailnet ACL, not the box, decides who gets in. This replaces the rule of commit `80f8327`, "keep tailnet to sshd only", which opened TCP 22 on `tailscale0` and nothing else. That rule is how the change was found: a service the agent started on `<tailnet-ip>:5137` could not be reached from the Mac over the tailnet, because `nixos-fw` dropped every port but 22 on `tailscale0`. The decision is the operator's: reaching the box's services over the tailnet with no further setup is worth more than a second filter behind the ACL.

What trusting the interface opens, all of it to whatever the ACL admits:

- every TCP port the agent binds on a wildcard or tailnet address, not only 22;
- UDP, which the old rule never admitted except the tunnel's own 41641;
- IPv6 arriving on `tailscale0`;
- tailscaled's peerAPI listeners on the tailnet addresses;
- every rootless podman port published on `0.0.0.0` (see [nix/podman.nix](podman.nix)), because a wildcard bind also answers on `tailscale0`.

Other nodes the ACL admits, such as other yolobox VMs, each running their own agents, reach all of it. Tighten the ACL, not the box, to narrow that.

The enp0s1 side is unchanged: `nixos-fw` opens only TCP 22 there (see [sshd on two interfaces](#sshd-on-two-interfaces)), and the tunnel's UDP 41641 stays open through `services.tailscale.openFirewall = true`.

`--netfilter-mode=off` in `extraSetFlags` stays, so that `nixos-fw` is the only thing that writes rules. By default tailscaled installs its own `ts-input` chain. The live `iptables -S` showed `-A INPUT -j ts-input` before `-A INPUT -j nixos-fw`, with `-A ts-input -i tailscale0 -j ACCEPT` inside it. That accept happened to match the trust given now, but it re-inserts its jump at the top of `INPUT` whenever tailscaled restarts, which is why `iptables -I INPUT 1` was never a fix for anything here, and it would keep a rule outside the declarative firewall. Netfilter mode off also drops tailscaled's anti-spoofing rule for 100.64/10, which the `ts-input` chain used to carry.

Recognise the setup with `sudo iptables -S INPUT | head -3`, which must show no `ts-input` jump, and `sudo nft list ruleset | grep 'iifname "tailscale0"'`, which must show a counter and a `jump nixos-fw-accept`.

## sshd on two interfaces

`nix/base.nix` sets `services.openssh.openFirewall = false`, so NixOS opens no port for sshd by default. sshd is reachable on exactly two interfaces: `tailscale0`, which is trusted whole (see [Firewall](#firewall); sshd takes keys only, see [Two accounts](../docs/accounts.md#two-accounts)), and `enp0s1`, where `nix/base.nix` opens port 22 and nothing else. `enp0s1` is lima's NAT network, 192.168.5.15. The gateway 192.168.5.2 is the Mac, and nothing on the LAN can reach that subnet. The per-interface list is declarative and works on the iptables and nftables backends alike. An earlier approach, an `ip46tables ... -j DROP` in `extraCommands` plus an assertion against nftables, did not.

The `enp0s1` rule exists because lima's hostagent can reach the guest's sshd over TCP on that address. Its readiness wait polls 192.168.5.15:22 before `limactl start` reports the instance up, and its usernet forwarder dials the same address. Under limactl 2.2.1 the hostagent takes one of two paths at start.

1. When it detects sshd on the vsock port, it logs `Started vsock forwarder: 127.0.0.1:60022 -> vsock:22 on VM` and `Detected SSH server is listening on the vsock port; changed 127.0.0.1:60022 to proxy for the vsock port`. It never waits on TCP. The 2026-10-24 boot did this 17 s after start.
2. Otherwise it waits up to 600 s on TCP 192.168.5.15:22, then falls back to its usernet forwarder, which dials the same address.

On 2026-10-22 the log carried no vsock line at all, and setting `ssh.overVsock: true` on the instance (`limactl edit yolobox --set '.ssh.overVsock = true'`) did not change that. Why detection missed that day is not known. With 22 open on `enp0s1` the TCP path succeeded and the box came up. So the TCP fallback is real, and `enp0s1` must allow 22 whatever `ssh.overVsock` says. The earlier belief that `enp0s1` needs nothing was wrong.

The rule was added for the first boot under limactl 2.2.1 after `80f8327`, which had opened 22 on `tailscale0` only. That boot died in `yo up` after 600 s with `timed out waiting for port to become available on 192.168.5.15:22`, then `did not receive an event with the running status`. `~/.lima/yolobox/ha.stderr.log` showed the wait starting at 16:02:43 and timing out at 16:12:37, then `Failed to wait for the guest SSH server to become available, falling back to usernet forwarder`. Every dial to 192.168.5.15:22 ended in `context deadline exceeded`, not `connection refused`, because `nixos-fw` dropped the packets. `serialv.log` ended at `yolobox login:`, so the guest itself was healthy. Recognise the shape by timeouts rather than refusals, with the guest at its login prompt. The `Can't proxy a datagram to udp ... 127.0.0.1:5351` lines in the same output are tailscaled noise.

The box was recovered over the tailnet, where 22 stayed open (`ssh -i ~/.lima/_config/user <operator>@<tailnet IP>`). The working tree was tarred in and switched with `nixos-rebuild switch --impure --flake path:...#yolobox`. A box with no tailnet login would have had no way in, since root and agent are locked and the serial console is output-only.

tailscaled's own ssh server, which `tailscale up --ssh` would start, authorises by tailnet ACL instead of by this repo's keys. `extraSetFlags` therefore carries `--ssh=false`.

Checks in the VM:

- `sudo iptables -S nixos-fw | grep 'dport 22'` must show exactly one accept, with `-i enp0s1`. The tailnet needs none, because `tailscale0` jumps to `nixos-fw-accept` before the port rules.
- `ss -a --vsock | grep ':22'` shows `v_str LISTEN *:22` whenever ssh rides vsock. The operator's sessions then appear as `ESTAB 3:22 <-> 2:*` (CID 2 is the host), and `ss -tan` shows no established connection on port 22 during a `yo ssh`. The Mac's `127.0.0.1:60022` (`ssh.localPort`) is the hostagent's listener on the Mac side.

Checks from a tailnet device: `ssh <user>@yolobox.<tailnet>.ts.net` must reach sshd and offer keys, and a password prompt is a bug. a service the agent binds on a wildcard address, say `python3 -m http.server 8080`, must answer `curl http://yolobox.<tailnet>.ts.net:8080`. The tailnet ACLs for the node are not reviewed (see `TODO.md`).

## tailscaled-set

A changed `extraSetFlags` has to reach the running box on `switch`, and for a while it did not. nixpkgs declares `tailscaled-set`, the unit that runs `tailscale set <flags>`, as a bare `Type=oneshot`. It is `inactive` once it has exited, and `switch-to-configuration` restarts a changed unit only if it is active. So the switch that added `--netfilter-mode=off` listed no `tailscaled-set` among its restarts and left the box exposed until the next boot. `systemctl is-active tailscaled-set` printed `inactive`, `tailscale debug prefs` still showed `"NetfilterMode": 2`, and `iptables -S INPUT` still had `-A INPUT -j ts-input` ahead of `nixos-fw`.

`nix/tailscale.nix` now sets `RemainAfterExit = true` on it, so the unit stays `active (exited)` and a changed flag list restarts it. The switch that first introduces `RemainAfterExit` cannot restart a unit that is still inactive. On a box that predates it, run `yo ssh sudo systemctl restart tailscaled-set` once. `systemctl show tailscaled-set -p RemainAfterExit -p ActiveState` must then answer `yes` and `active`.

## DNS and logging

`--accept-dns=false` stops MagicDNS from taking over `/etc/resolv.conf`. At tailscale's default the guest's DNS would answer through the tailnet's resolver for every name, not only `*.ts.net`. That would silently change what every other process in the box resolves, including the Mac's own DNS filter that the AWS setup relies on staying in place (see [The DNS sinkhole](../docs/host-network.md#the-dns-sinkhole)). The box's own name is still reachable as `yolobox.<tailnet>.ts.net`, because MagicDNS runs on the client end of a tailnet connection, never in this guest's resolver.

`disableUpstreamLogging = true` sets `TS_NO_LOGS_NO_SUPPORT=true` on tailscaled. The node uploads no logs to log.tailscale.com, and for the same reason Tailscale support will not help with this node. It is working when `yo ssh journalctl -u tailscaled -b` shows `You have disabled logging`. It takes effect on the next switch, because the variable sits in the unit's environment and a changed unit makes the switch restart tailscaled.

## Relayed contacts in the log

`journalctl -u tailscaled` shows `magicsock: new contact: peer=[+1OFL] usec=... cached=false via=derp` over and over, every few seconds for as long as a burst lasts. The line is not an error and not a reconnect. In tailscale v1.104.1 (`wgengine/magicsock/magicsock.go`) it is logged each time a peer's disco call-me-maybe arrives over DERP while the box holds no direct path to that peer (`if !lastBest.ap.IsValid()`). A peer that wants a direct path with us sends one every few seconds until its pings are answered, so the line repeats exactly as long as the path stays relayed. The direct twin, `via=direct`, is logged once when a direct path is first adopted.

The peer in the short form `[+1OFL]` is the first five characters of the base64 of its node key, while `tailscale status --json` prints the key as hex. Convert before matching: `python3 -c 'import base64,sys; print(base64.b64encode(bytes.fromhex(sys.argv[1])).decode()[:5])' <hex after "nodekey:">`. On the box where this was written the peer varied: two other nodes during the first hours.

Measured on a live box: 706 such lines in 3.7 hours (about three a minute), roughly 100 bytes each. That is about 0.04 percent of the journal's size. The 2.8 GiB journal is container output (see TODO.md), so the line is noise in `journalctl -u tailscaled` and not a disk problem. Rules out, in turn:

- The Mac's own Tailscale node as the peer: it appeared twice in the whole boot.
- The UDP 41641 collision in [lima/yolobox.md](../lima/yolobox.md#guest-udp-is-never-mirrored): that is lima's mirror on the Mac's loopback, which no packet of tailscaled's takes.

What decides whether a direct path exists is outside this repo. The box reaches the internet through lima's NAT on the Mac, so a peer can use only addresses the Mac's network lets through. One cause was found and it is on the Mac: a VPN that disconnects leaves lima's UDP flows bound to the VPN's vanished address, tailscaled's UDP then goes nowhere, and netcheck logs `UDP is blocked, trying HTTPS` every 25 seconds. That is described in [docs/host-network.md](../docs/host-network.md#a-vpn-that-disconnects-leaves-udp-flows-stuck). Clearing it needs a host agent restart on the Mac, so no setting in the guest could.

tailscaled has no switch for this line, and its verbosity flags only add lines. So `nix/tailscale.nix` sets `LogFilterPatterns` on the unit to drop exactly `via=derp`, a mitigation and not a fix. systemd's journald applies it to the unit's stream, so it also holds for tailscaled's stderr. The `via=direct` line, `now using` (the path change) and `endpoints changed` all stay, because they report events. `TestTailscaledLogFilter` checks the pattern against real lines. Drop the setting if tailscale ever ratelimits or demotes the line. To see the relayed state without it, use `tailscale status` (`relay "..."` and no `direct`) or `tailscale ping <node>`, which answers `via DERP(...)` or `via <ip:port>`.

## The port mapper

`nix/tailscale.nix` sets `TS_DISABLE_PORTMAPPER=1` on tailscaled. Its port mapper sends NAT-PMP and PCP to 192.168.5.2:5351 and SSDP to 239.255.255.250, and both go only to the host and the LAN, which the [egress table](egress.md) rejects. Without the setting tailscaled would keep probing a path that cannot answer. A peer on the same LAN then connects through public endpoints and then DERP, which is slower (tailscale.com/kb/1257).

## Login

Login is interactive and happens once, by the operator, because no auth key exists anywhere in this repo: `yo ssh sudo tailscale up`. It takes no `--hostname`. `extraSetFlags` pins the tailnet name to `networking.hostName`, so every box joins as `yolobox`, and a hand-typed `--hostname` is overwritten by the next `tailscaled-set` run.

`--netfilter-mode=off`, `--accept-dns=false` and `--hostname` are all non-default, so a bare `tailscale up` on an already logged-in node may refuse with "changing settings via 'tailscale up' requires mentioning all non-default flags". Do not answer that with `tailscale up --reset`, which clears them. Run `yo ssh sudo tailscale up`, then `yo ssh sudo systemctl restart tailscaled-set`, so the unit re-applies the flags.

State from the login, the node key and the tailnet identity, persists under `/var/lib/tailscale`. Like everything else under [Two accounts](../docs/accounts.md#two-accounts), it is VM-local. Recreating the VM loses it, and the new box logs in again the same way.

## Box-local routes

The first switch into a generation with this module restarts dhcpcd, because nixpkgs' tailscale module adds `tailscale0` to `networking.dhcpcd.denyInterfaces`, which changes dhcpcd's config. On stop dhcpcd removes the lease address, and the kernel flushes every route that hung off it. That includes any static route a `/etc/yolobox/local.nix` declared through `networking.interfaces.<if>.ipv4.routes`, because its `network-addresses-<if>` unit runs once and never re-adds it. The switch reports success and the route is simply gone. Any later dhcpcd restart, such as a nixpkgs bump, does the same. A box-local route therefore belongs in `networking.dhcpcd.runHook`, re-added with `ip route replace` on every `BOUND`, `REBOOT`, `RENEW` and `REBIND`, so its lifetime follows the lease it depends on.

## The control socket

tailscaled's LocalAPI socket is `/run/tailscale/tailscaled.sock`. tailscaled chmods that socket 0666 itself (`safesocket/unixsocket.go`), so the socket's own mode lets anyone in. What restricts it is the directory. systemd creates `/run/tailscale` from `RuntimeDirectoryMode`, and tailscaled does not re-chmod a directory that exists. `nix/tailscale.nix` sets `RuntimeDirectoryMode = "0750"` and an `ExecStartPre` that runs `chgrp wheel /run/tailscale`, so the directory is `root:wheel` `0750`. The directory exists before `ExecStartPre` runs.

The operator is in `wheel`, so `yo status` can run `tailscale status` as the operator. The agent is uid 1000, group `users`, not in `wheel`, and gets `permission denied` from `tailscale status`. Recognise the setup with `yo ssh 'sudo -u agent tailscale status; echo rc=$?'`, which must print `permission denied` and a non-zero `rc`, and `yo ssh stat -c '%a %U:%G' /run/tailscale`, which must print `750 root:wheel`.
