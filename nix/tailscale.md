# Tailscale

The code is `nix/tailscale.nix`, which puts the box on a tailnet so that its services are reachable from outside the host's own LAN. Tailscale is optional: a box that never runs `tailscale up` works as before, and nothing in boot waits for a login. The comments in that file give the short reason for each setting. This file holds the evidence and how to recognise each failure.

## Firewall

`tailscale0` is a trusted interface (`networking.firewall.trustedInterfaces = [ "tailscale0" ]`), so `nixos-fw` accepts everything that arrives on it. The tailnet ACL, not the box, decides who gets in. The alternative, "tailnet to sshd only", opens TCP 22 on `tailscale0` and nothing else. Its symptom is a service the agent starts on `<tailnet-ip>:5137` that cannot be reached over the tailnet, because `nixos-fw` drops every port but 22 on `tailscale0`. The design is that reaching the box's services over the tailnet with no further setup is worth more than a second filter behind the ACL. The cost is that the ACL is the only access control: a box on a tailnet should have its ACL reviewed, because every node the ACL admits reaches every port of the box, including whatever the agent binds on a wildcard address.

What trusting the interface opens, all of it to whatever the ACL admits:

- every TCP port the agent binds on a wildcard or tailnet address, not only 22;
- UDP, which a "sshd only" rule would not admit;
- IPv6 arriving on `tailscale0`;
- tailscaled's peerAPI listeners on the tailnet addresses;
- every rootless podman port published on `0.0.0.0` (see [nix/podman.nix](podman.nix)), because a wildcard bind also answers on `tailscale0`.

Other nodes the ACL admits, such as other yolobox VMs, each running their own agents, reach all of it. Tighten the ACL, not the box, to narrow that. The other direction is the same: the egress table accepts everything that leaves by `tailscale0`, so a host that is a tailnet node is reachable from the box unless the ACL forbids it (see [The tailnet is an entrance](egress.md#the-tailnet-is-an-entrance)).

The enp0s1 side is unchanged: `nixos-fw` opens only TCP 22 there (see [sshd on two interfaces](#sshd-on-two-interfaces)), and `services.tailscale.openFirewall` is `false`. Setting it to `true` would open the tunnel's UDP port on `enp0s1` too, which breaks the rule that the interface admits port 22 only. Direct tailnet paths are negotiated by outbound traffic that conntrack then lets back in, and a path that cannot be made direct goes through a relay, so closing the port should cost speed at worst, not reachability. That is reasoning, not a measurement: if a box shows `via DERP` for a peer on the same network where it used to be direct, this is the first place to look.

`--netfilter-mode=off` in `extraSetFlags` stays, so that `nixos-fw` is the only thing that writes rules. By default tailscaled installs its own `ts-input` chain: `-A INPUT -j ts-input` before `-A INPUT -j nixos-fw`, with `-A ts-input -i tailscale0 -j ACCEPT` inside it. That accept happens to match the trust given here, but tailscaled re-inserts its jump at the top of `INPUT` whenever it restarts, so a hand-added `iptables -I INPUT 1` is no fix for anything, and the chain would keep a rule outside the declarative firewall. Netfilter mode off also drops tailscaled's anti-spoofing rule for 100.64/10, which the `ts-input` chain used to carry.

Recognise the setup with `sudo iptables -S INPUT | head -3`, which must show no `ts-input` jump, and `sudo nft list ruleset | grep 'iifname "tailscale0"'`, which must show a counter and a `jump nixos-fw-accept`.

## sshd on two interfaces

`nix/base.nix` sets `services.openssh.openFirewall = false`, so NixOS opens no port for sshd by default. sshd is reachable on exactly two interfaces: `tailscale0`, which is trusted whole (see [Firewall](#firewall); sshd takes keys only, see [Two accounts](../docs/accounts.md#two-accounts)), and `enp0s1`, where `nix/base.nix` opens port 22 and nothing else. `enp0s1` is lima's NAT network, 192.168.5.15. The gateway 192.168.5.2 is the host, and nothing on the LAN can reach that subnet. The per-interface list is declarative and works on the iptables and nftables backends alike, which an `ip46tables ... -j DROP` in `extraCommands` does not.

The `enp0s1` rule exists because lima's hostagent can reach the guest's sshd over TCP on that address. Its readiness wait polls 192.168.5.15:22 before `limactl start` reports the instance up, and its usernet forwarder dials the same address. Under limactl 2.2.1 the hostagent takes one of two paths at start.

1. When it detects sshd on the vsock port, it logs `Started vsock forwarder: 127.0.0.1:60022 -> vsock:22 on VM` and `Detected SSH server is listening on the vsock port; changed 127.0.0.1:60022 to proxy for the vsock port`. It never waits on TCP, and the line appears within seconds of the start.
2. Otherwise it waits up to 600 s on TCP 192.168.5.15:22, then falls back to its usernet forwarder, which dials the same address.

Detection can miss, and `ssh.overVsock: true` on the instance does not make it hit. The TCP fallback is therefore real, and `enp0s1` must allow 22 whatever `ssh.overVsock` says.

Symptom when 22 is closed on `enp0s1` (open on `tailscale0` only): `yo up` dies after 600 s with `timed out waiting for port to become available on 192.168.5.15:22`, then `did not receive an event with the running status`. `~/.lima/yolobox/ha.stderr.log` shows the wait starting and, 600 s later, timing out, then `Failed to wait for the guest SSH server to become available, falling back to usernet forwarder`. Every dial to 192.168.5.15:22 ends in `context deadline exceeded`, not `connection refused`, because `nixos-fw` drops the packets. `serialv.log` ends at `yolobox login:`, so the guest itself is healthy. Recognise the shape by timeouts rather than refusals, with the guest at its login prompt. The `Can't proxy a datagram to udp ... 127.0.0.1:5351` lines in the same output are tailscaled noise.

Recovery needs another way in, and no box has one. `AllowUsers` admits the operator only from 192.168.5.2, on every box, so the operator's login from the tailnet is refused even on a box that is logged in to one (see "No host OS in the guest" in [nix/base.md](base.md#no-host-os-in-the-guest)). Neither account may log in from the tailnet (`AllowUsers` admits both from 192.168.5.2 only), root is locked and the serial console is output-only. So a box whose 22 on `enp0s1` is closed is recreated (`yo`'s delete remedy prints the commands), and the change that closed it is reverted first.

tailscaled's own ssh server, which `tailscale up --ssh` would start, authorises by tailnet ACL instead of by this repo's keys. `extraSetFlags` therefore carries `--ssh=false`.

Checks in the VM:

- `sudo iptables -S nixos-fw | grep 'dport 22'` must show exactly one accept, with `-i enp0s1`. The tailnet needs none, because `tailscale0` jumps to `nixos-fw-accept` before the port rules.
- `ss -a --vsock | grep ':22'` shows `v_str LISTEN *:22` whenever ssh rides vsock. The operator's sessions then appear as `ESTAB 3:22 <-> 2:*` (CID 2 is the host), and `ss -tan` shows no established connection on port 22 during a `yo ssh`. The host's `127.0.0.1:60022` (`ssh.localPort`) is the hostagent's listener on the host side.

Checks from a tailnet device: `ssh agent@yolobox.<tailnet>.ts.net` must reach sshd and end in `Permission denied (publickey)`: both accounts are refused off the gateway by design (`AllowUsers` admits both from 192.168.5.2 only), and a password prompt is a bug. A service the agent binds on a wildcard address, say `python3 -m http.server 8080`, must answer `curl http://yolobox.<tailnet>.ts.net:8080`. Review the tailnet ACL for the node: every node it admits reaches every port of the box (see [Firewall](#firewall)).

## tailscaled-set

A changed `extraSetFlags` has to reach the running box on `switch`. nixpkgs declares `tailscaled-set`, the unit that runs `tailscale set <flags>`, as a bare `Type=oneshot`. It is `inactive` once it has exited, and `switch-to-configuration` restarts a changed unit only if it is active. So without a fix a switch that changes the flags lists no `tailscaled-set` among its restarts and leaves the old flags in force until the next boot. Symptom: `systemctl is-active tailscaled-set` prints `inactive`, `tailscale debug prefs` still shows the old value (for example `"NetfilterMode": 2` after `--netfilter-mode=off` was added), and `iptables -S INPUT` still has `-A INPUT -j ts-input` ahead of `nixos-fw`.

`nix/tailscale.nix` sets `RemainAfterExit = true` on it, so the unit stays `active (exited)` and a changed flag list restarts it. The switch that first introduces `RemainAfterExit` cannot restart a unit that is still inactive. On a box that predates it, run `yo ssh sudo systemctl restart tailscaled-set` once. `systemctl show tailscaled-set -p RemainAfterExit -p ActiveState` must then answer `yes` and `active`.

## DNS and logging

`--accept-dns=false` stops MagicDNS from taking over `/etc/resolv.conf`. At tailscale's default the guest's DNS would answer through the tailnet's resolver for every name, not only `*.ts.net`. That would silently change what every other process in the box resolves, including any filtering the host's own DNS does, which the AWS setup relies on staying in place (see [The DNS sinkhole](../docs/host-network.md#the-dns-sinkhole)). The box's own name is still reachable as `yolobox.<tailnet>.ts.net`, because MagicDNS runs on the client end of a tailnet connection, never in this guest's resolver.

`disableUpstreamLogging = true` sets `TS_NO_LOGS_NO_SUPPORT=true` on tailscaled. The node uploads no logs to log.tailscale.com, and for the same reason Tailscale support will not help with this node. It is working when `yo ssh journalctl -u tailscaled -b` shows `You have disabled logging`. It takes effect on the next switch, because the variable sits in the unit's environment and a changed unit makes the switch restart tailscaled.

## Relayed contacts in the log

`journalctl -u tailscaled` shows `magicsock: new contact: peer=[abcde] usec=... cached=false via=derp` over and over, every few seconds for as long as a burst lasts. The line is not an error and not a reconnect. In tailscale's `wgengine/magicsock/magicsock.go` it is logged each time a peer's disco call-me-maybe arrives over DERP while the box holds no direct path to that peer (`if !lastBest.ap.IsValid()`). A peer that wants a direct path with us sends one every few seconds until its pings are answered, so the line repeats exactly as long as the path stays relayed. The direct twin, `via=direct`, is logged once when a direct path is first adopted.

The peer in the short form `[abcde]` is the first five characters of the base64 of its node key, while `tailscale status --json` prints the key as hex. Convert before matching: `python3 -c 'import base64,sys; print(base64.b64encode(bytes.fromhex(sys.argv[1])).decode()[:5])' <hex after "nodekey:">`.

The line is noise in `journalctl -u tailscaled` and not a disk problem; a journal that grows large is container output (see [podman.md](podman.md)). Not the cause:

- The UDP 41641 collision in [lima/yolobox.md](../lima/yolobox.md#guest-udp-is-never-mirrored): that is lima's mirror on the host's loopback, which no packet of tailscaled's takes.

What decides whether a direct path exists is outside this repo. The box reaches the internet through lima's NAT on the host, so a peer can use only addresses the host's network lets through. One cause was found and it is on the host: a VPN that disconnects leaves lima's UDP flows bound to the VPN's vanished address, tailscaled's UDP then goes nowhere, and netcheck logs `UDP is blocked, trying HTTPS` every 25 seconds. That is described in [docs/host-network.md](../docs/host-network.md#a-vpn-that-disconnects-leaves-udp-flows-stuck). Clearing it needs a host agent restart, so no setting in the guest could. On a Linux host a second cause was the tunnel's fixed port, which passt could not bind on the host; see [The tunnel port](#the-tunnel-port).

tailscaled has no switch for this line, and its verbosity flags only add lines. So `nix/tailscale.nix` sets `LogFilterPatterns` on the unit to drop exactly `via=derp`, a mitigation and not a fix. systemd's journald applies it to the unit's stream, so it also holds for tailscaled's stderr. The `via=direct` line, `now using` (the path change) and `endpoints changed` all stay, because they report events. `TestTailscaledLogFilter` checks the pattern against real lines. Drop the setting if tailscale ever ratelimits or demotes the line. To see the relayed state without it, use `tailscale status` (`relay "..."` and no `direct`) or `tailscale ping <node>`, which answers `via DERP(...)` or `via <ip:port>`.

## The port mapper

`nix/tailscale.nix` sets `TS_DISABLE_PORTMAPPER=1` on tailscaled. Its port mapper sends NAT-PMP and PCP to 192.168.5.2:5351 and SSDP to 239.255.255.250, and both go only to the host and the LAN, which the [egress table](egress.md) rejects. Without the setting tailscaled would keep probing a path that cannot answer. A peer on the same LAN then connects through public endpoints and then DERP, which is slower (tailscale.com/kb/1257).

## The tunnel port

`services.tailscale.port` is `0`, so tailscaled picks a free UDP port each time it starts. NixOS defaults to 41641, and that default broke direct paths on Linux hosts.

A Linux host runs the box under libvirt, with passt for its network (see [docs/linux-host.md](../docs/linux-host.md#why-qemusystem-and-passt)). The host often runs Tailscale too, and its tailscaled binds UDP `0.0.0.0:41641` and `[::]:41641`. The box's tailscaled sends from UDP 41641 as well. passt keeps the guest's source port when it opens the host-side socket for a UDP flow, so that bind fails with `EADDRINUSE` and passt drops the datagram. The failure writes these lines to the host journal, once per datagram; one box produced about 98,000 of them:

```
passt[PID]: Flow 0 (UDP flow): Couldn't open flow specific socket
passt[PID]: : Address already in use
passt[PID]: Dropping datagram with no flow TAP 192.168.5.15:41641 -> 51.15.24.89:3478
```

The box then has no direct UDP path, so STUN and disco cannot find one, and every tailnet peer is reached through DERP. Two boxes on one Linux host would collide the same way without any host Tailscale, because each box's passt would bind the same host port.

To recognise it, run `ss -ulpn | grep 41641` on the host. A socket on 41641 while the host's tailscaled is active means a box's datagrams from 41641 have nowhere to go. `journalctl _COMM=passt` on the host shows the three lines above. In the box, `tailscale status` shows peers only as relays (`relay "..."` and no `direct`).

Port 0 removes the fixed port that passt would bind, so nothing collides. A different fixed port would only move the collision: two boxes on one host would still send from the same port. The setting applies to every box, and no host check gates it, because the guest is never told which host runs it (see [No host OS in the guest](base.md#no-host-os-in-the-guest)). On a Mac the port is NATed by Lima and never mirrored (see [lima/yolobox.md](../lima/yolobox.md#guest-udp-is-never-mirrored)), so the change makes no visible difference there. A box built before this change keeps 41641 until it is rebuilt; see [docs/linux-host.md](../docs/linux-host.md#recognising-failures).

## Exit nodes

`--exit-node-allow-lan-access` is in `extraSetFlags`. When an exit node is selected, tailscaled sends all traffic through table 52 to the exit node, and without this flag that includes the replies to Lima's ssh on 192.168.5.0/24, so `yo ssh` drops. The flag keeps that subnet out of table 52. It does nothing until an exit node is chosen. It does not open the LAN: the [egress table](egress.md) still rejects private ranges except the networks named in `yolobox.network.lanAllow`, which are reachable as before. Lima's ssh replies on 192.168.5.0/24 pass as replies to connections the outside started (`ct direction reply`).

With the flag set and a tailnet exit node selected, `tailscale debug prefs` shows `ExitNodeAllowLANAccess: true`, `ip route show table 52` holds `throw 192.168.5.0/24` and `throw 127.0.0.0/8`, and `yo ssh` keeps working for as long as the exit node is selected.

Not tested: whether `yo ssh` drops without the flag. The expectation is that it does, because table 52 then carries the replies to Lima's ssh. To check, build a box without the flag, run `yo ssh sudo tailscale set --exit-node=<node>`, and see whether `yo ssh` stays up.

## Login

Login is interactive and happens once, by the operator, because no auth key exists anywhere in this repo: `yo ssh sudo tailscale up`. It takes no `--hostname`. `extraSetFlags` pins the tailnet name to `networking.hostName`, so the default machine joins as `yolobox` and `yo -m NAME` machines as `yolobox-<name>` (lowercased; see [Hostname](base.md#hostname)), and a hand-typed `--hostname` is overwritten by the next `tailscaled-set` run. A named machine built before that variable existed was `yolobox` on the tailnet; its next bootstrap renames the node, and `<old>.ts.net` names stop resolving.

`--netfilter-mode=off`, `--accept-dns=false`, `--exit-node-allow-lan-access` and `--hostname` are all non-default, so a bare `tailscale up` on an already logged-in node may refuse with "changing settings via 'tailscale up' requires mentioning all non-default flags". Do not answer that with `tailscale up --reset`, which clears them. Run `yo ssh sudo tailscale up`, then `yo ssh sudo systemctl restart tailscaled-set`, so the unit re-applies the flags.

State from the login, the node key and the tailnet identity, persists under `/var/lib/tailscale`. Like everything else under [Two accounts](../docs/accounts.md#two-accounts), it is VM-local. Recreating the VM loses it, and the new box logs in again the same way.

## Box-local routes

The first switch into a generation with this module restarts dhcpcd, because nixpkgs' tailscale module adds `tailscale0` to `networking.dhcpcd.denyInterfaces`, which changes dhcpcd's config. On stop dhcpcd removes the lease address, and the kernel flushes every route that hung off it. That includes any static route a `/etc/yolobox/local.nix` declared through `networking.interfaces.<if>.ipv4.routes`, because its `network-addresses-<if>` unit runs once and never re-adds it. The switch reports success and the route is simply gone. Any later dhcpcd restart, such as a nixpkgs bump, does the same. A box-local route therefore belongs in `networking.dhcpcd.runHook`, re-added with `ip route replace` on every `BOUND`, `REBOOT`, `RENEW` and `REBIND`, so its lifetime follows the lease it depends on.

## The control socket

tailscaled's LocalAPI socket is `/run/tailscale/tailscaled.sock`. tailscaled chmods that socket 0666 itself (`safesocket/unixsocket.go`), so the socket's own mode lets anyone in. What restricts it is the directory. systemd creates `/run/tailscale` from `RuntimeDirectoryMode`, and tailscaled does not re-chmod a directory that exists. `nix/tailscale.nix` sets `RuntimeDirectoryMode = "0750"` and an `ExecStartPost` that runs `chgrp wheel /run/tailscale`, so the directory is `root:wheel` `0750`.

The chgrp is `ExecStartPost` and cannot be `ExecStartPre`. systemd (`systemd.exec(5)`, `RuntimeDirectory=`) makes the runtime directory owned by the unit's `User=` and `Group=`, here root:root, and it does so in every process it spawns for the unit, including `+` commands (`exec_invoke` calls `setup_exec_directory` for each command, and the loop is not conditioned on the `+` flag). The man page does not say "per command"; the source does. A chgrp in `ExecStartPre` therefore runs first, and the later `ExecStartPre` gates (`nft list table ...`) and `ExecStart` put the group back to root. The unit is `Type=notify`, so `ExecStartPost` runs after readiness, after every other spawn. A `Restart=` re-runs the whole start sequence, `ExecStartPost` included. Between the spawn and the chgrp the directory is `root:root` `0750`, which is stricter than the final state, so that window fails closed for the agent.

Symptom of a chgrp in `ExecStartPre`: `stat` shows `drwxr-x--- root root /run/tailscale`, the operator gets `permission denied` on `/run/tailscale/tailscaled.sock`, and `yo status` prints `tailscale: WARNING status unreadable ... permission denied`. The fix is the chgrp in `ExecStartPost`.

The operator is in `wheel`, so `yo status` can run `tailscale status` as the operator. The agent is uid 1000, group `users`, not in `wheel`, and gets `permission denied` from `tailscale status`. Recognise the setup with `yo ssh 'sudo -u agent tailscale status; echo rc=$?'`, which must print `permission denied` and a non-zero `rc`, and `yo ssh stat -c '%a %U:%G' /run/tailscale`, which must print `750 root:wheel`.
