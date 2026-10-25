# The kill switch

The code is `nix/killswitch.nix` (the nft table, the DNS assertion and `/etc/yolobox/network.json`) and, for the exit node, `nix/tailscale.nix`. It is optional. A box that opts in (see [Opting in](#opting-in)) gets one promise kept: the box does not reach the internet directly through the Mac. The only way out is a tailnet exit node, and DNS is asked of a resolver on the tailnet. A box that does not opt in has none of this and behaves as a box without a kill switch. Once a box has opted in there is no switch to turn the table off, because an off switch would make the promise conditional. Leaving the opt-in is the only way out of it. The tailnet itself is in [Tailscale](tailscale.md), and what the Mac's network can do to tailscaled's own packets is in [The Mac's network](../docs/mac-network.md#with-the-kill-switch-armed).

The rules were derived from nixpkgs and the kernel, and rehearsed in throwaway network namespaces (see [Rehearsing](#rehearsing)). Anything a rehearsal cannot show is marked as not yet exercised on a live box, and `TODO.md` lists it.

## Opting in

The kill switch is one operator's network policy, so the repo does not impose it. The switch is `yolobox.network.exitNode`, which is null by default. With it null, `nix/killswitch.nix` adds nothing: no nft table, no `nohook resolv.conf` on dhcpcd, no nameserver assertion, no `/etc/yolobox/network.json`, no `dhcpcd` or `tailscaled` gate, and no exit-node unit in `nix/tailscale.nix`. The module wraps its whole config in `lib.mkIf (cfg.exitNode != null)`, and `TestKillSwitchIsOptIn` in `tests/test_consistency.py` pins that.

A box opts in by naming two values in `/etc/yolobox/local.nix`, which is per box and never in the repo. `nix/base.nix` imports that file when it exists.

- `yolobox.network.exitNode`: the tailnet host name of the exit node, as `tailscale status` shows it (letters, digits and dashes).
- `networking.nameservers`: the stock NixOS option. It must name a resolver reachable over the tailnet, such as the exit node itself, and it must not be empty or contain the Lima gateway `192.168.5.2`. The build asserts both, and only when the box opted in.

For example:

```nix
{ ... }: {
  yolobox.network.exitNode = "my-exit-node";
  networking.nameservers = [ "100.101.102.103" ];
}
```

Opting in has prerequisites on the tailnet, which are the operator's to meet and are not yolobox's: an exit node that advertises itself and is approved in the admin console, an access policy that lets this node reach the internet through it ([The tailnet policy](#the-tailnet-policy)), and a resolver on the tailnet. Without the first two the box has no egress at all.

`/etc/yolobox/network.json` is `{"exitNode": "<name>"}`, written only for a box that opted in, and it is the only thing `yolobox-guest egress-status` and `net-check` trust. Its absence means the box did not opt in: `egress-status` says `kill switch not enabled` and exits 0, `yo status` shows that line, and `net-check` probes the Lima gateway as it always did. See [egress-status](guest/yolobox-guest.md#egress-status-the-kill-switchs-verdict).

### Leaving the opt-in

Remove the two lines from `local.nix` and rebuild. The generation then has no kill switch unit, no `dhcpcd` or `tailscaled` gate and no `network.json`, but the table the old generation loaded is in the kernel and has no owner, so it stays until the next boot (the same reason it has no stop hook). Restart the VM, or delete it with the break-glass below. The exit node tailscaled remembered stays in its prefs until someone runs `tailscale set --exit-node=`.

## The table and why it has no stop hook

`yolobox-killswitch` loads `table inet yolobox-killswitch` with one `nft --file`. The file says `table X`, `delete table X`, `table X { ... }`, which nft applies as a single transaction, so a reload has no instant at which the table is missing and the unit is safe to restart.

It is a separate table, not `networking.nftables`. That option would switch the firewall backend, and `nixos-fw` stays on iptables-nft (see [Firewall](tailscale.md#firewall)).

The unit has no `ExecStop`, deliberately. A stop hook that deletes the table opens the box whenever the unit stops, and `switch-to-configuration` stops and starts a changed unit. A table with no owner is only ever deleted by a person, and the break-glass for that person is:

```sh
sudo sh -c 'nft delete table inet yolobox-killswitch; systemd-run --on-active=15m systemctl restart yolobox-killswitch'
```

It opens the box and arms a timer that closes it again after fifteen minutes. There is no `yo` command for it on purpose: a command the agent could call is a command that opens the box. While the table is absent, dhcpcd and tailscaled will not start (see [Failing closed](#failing-closed)), which is fine for the fifteen minutes because both are already running.

Order: `wantedBy = sysinit.target`, before `network-pre.target`, with `DefaultDependencies=false`, so the table exists before any ordinary service starts. There is no bootstrap exemption: `yo bootstrap` uses `nixos-rebuild boot`, so the table arms on the first boot of the new generation, after the build has finished. A live `nixos-rebuild test` from the previous generation starts `yolobox-killswitch.service` and `yolobox-tailscale-exit-node.service` as part of the switch. If a switch ever does not start it, `yolobox-guest egress-status` exits 2 with "kill switch not armed", and a reboot arms it.

## The rules

The output chain, in order:

1. `oifname { "lo", "tailscale0" } accept`: the way out.
2. `meta mark & 0x00ff0000 == 0x00080000 counter accept`: tailscaled's own packets. Its control connection, DERP and UDP 41641 must leave by the Lima interface, since they are what builds the tunnel the rest travels in. Two rejects come first, for marked packets addressed to the Lima link itself: `192.168.5.0/24` and IPv4 multicast, and IPv6 link-local and multicast. None of tailscaled's tunnel traffic goes there, but its port mapper does. Seen live with `tcpdump -ni enp0s1` before the rejects existed: NAT-PMP and PCP to `192.168.5.2:5351`, UPnP to `192.168.5.2:1900` and SSDP to `239.255.255.250:1900`, all with the mark. The gateway is gvproxy's NAT to the Mac's own loopback, so without the rejects tailscaled would be a guest-initiated path to the Mac.
3. `ct direction reply accept`: replies to flows started from outside, so `yo ssh` and lima's hostagent keep working.
4. `udp sport 68 udp dport 67 accept`, the three IPv6 neighbour discovery messages, and MLD listener reports to link-local groups (`ff02::/16`): dhcpcd and the link need them. The kernel sends MLD reports whenever an address joins a multicast group, so they are link housekeeping, and the `ff02::/16` scope keeps them from leaving the link.
5. A rate-limited log line, then `reject with icmpx type admin-prohibited`. Reject, not drop, so a blocked program fails at once with "administratively prohibited" instead of timing out. The log prefix `yolobox-killswitch: direct egress blocked:` names it in `journalctl -k`.

The forward chain accepts the same two interfaces and drops the rest, so a container network cannot route around it.

The priority is -10. A rerouting chain that ran after the kill switch could change a packet's interface after the kill switch judged it, and -10 runs after every rerouting hook (mangle at -150, dstnat at -100) and before the filter hooks at 0. Conntrack hooks at -200, so `ct direction reply` works. The numbers (mark, mask, priority, log rate) are `let` bindings at the top of the file.

Recognise the table with `sudo nft list table inet yolobox-killswitch`. Two counters show what it has done: the mark accept counts tailscaled's packets, and the reject counts everything blocked. `yolobox-guest egress-status` states the verdict in one line, and `yo status` shows it as `egress:`.

### Failing closed

If the rule file fails to load, the unit fails, and a unit that failed does not stop anything else from starting. Ordering (`before`) is not a requirement. Left alone, the box would come up with its DHCP address and no table: open.

So the units that give the box egress check the table themselves. `dhcpcd` (which supplies the box's address) and `tailscaled` have `ExecStartPre = nft list table inet yolobox-killswitch`, which exits non-zero when the table is absent, and the unit then does not start. They also `Want` and come `After` the kill switch, so on a normal boot or switch the table is loaded first. The price is that a box whose rule file is broken has no address and `yo ssh` cannot reach it. Lima's serial console still can, and CI makes that case rare by running `nft --check` on the rendered rule file ([verify.yml](../.github/workflows/verify.yml)).

It is `Wants`, not `Requires`, on purpose. A `Requires=` is passed on as a restart: whenever a switch restarts the kill switch, systemd would restart dhcpcd too, which drops the lease and with it the operator's ssh session. Rehearsed with transient units: with `Wants` and `After`, starting the dependent starts the kill switch first, restarting the kill switch leaves the dependent running, and a failed kill switch makes the dependent refuse to start.

What this does not cover: the initrd's network, which runs before the table exists and does nothing but unlock the disk, and a process that speaks on an address the box already has after the table was deleted by hand (that is the break-glass).

## DNS

On a box that opted in, plain DNS to the Lima gateway (192.168.5.2) is rejected, and the gateway is the Mac's resolver, so the box must not use it. DNS goes in plain DNS to a resolver reachable over the tailnet. WireGuard already encrypts that leg, so no DNS-over-TLS is added, and nothing else on the box resolves.

This is the stock `networking.nameservers`, and the build asserts it is non-empty and does not contain `192.168.5.2`. dhcpcd is told `nohook resolv.conf`, so the lease's resolver (the Lima gateway) and search domain never reach `/etc/resolv.conf`. openresolv then writes only the configured nameservers.

Once, after the first live switch from a generation without the kill switch (not at boot), the old dhcpcd's entry may still sit in openresolv's state. `/run/resolvconf/keys/enp0s1.dhcp` can keep `192.168.5.2` and the old search domain in `/etc/resolv.conf`, because the new dhcpcd no longer adds them and nothing removes the old entry. This is harmless, since the kill switch rejects `192.168.5.2`, and a reboot clears it because `/run` is tmpfs. To clear it live, delete the entry and regenerate the file: `sudo resolvconf -d enp0s1.dhcp && sudo resolvconf -u` (`-d` takes the key, `-u` rewrites `/etc/resolv.conf` from the remaining entries; the `static` entry for the configured nameservers stays).

tailscaled keeps `--accept-dns=false`. With MagicDNS on, the tailscale resolver would forward queries to upstream resolvers from tailscaled's own sockets, which carry the mark the kill switch lets out of the Lima interface, so DNS would leave the box directly. The box's own name is still reachable as `yolobox.<tailnet>.ts.net` from other nodes, since MagicDNS runs on their end.

Because the box no longer asks the Mac, the Mac's DNS filter does not apply to it (see [The DNS sinkhole](../docs/mac-network.md#the-dns-sinkhole)). On a cold boot the nameserver is reachable only after tailscaled and the exit node are up, so names fail until then. Seen on a live box: the first two queries to the tailnet resolver, a second after dhcpcd started, left by enp0s1 and were rejected (`journalctl -k` shows them with `DPT=53`), and names resolved once the exit node was selected. Container DNS through rootless podman's pasta, which forwards to the host's first nameserver, works on a live box: `podman run --rm docker.io/library/alpine:3.20 getent hosts github.com` answers. From the same container the Lima gateway and the Lima subnet are rejected, because pasta's sockets live in the box's own network namespace and meet the kill switch there.

## The exit node

`yolobox-tailscale-exit-node` runs `tailscale set --exit-node=<node> --exit-node-allow-lan-access` once tailscaled is `Running`. It waits the same way `yolobox-tailscale-serve` does (see [Serve readiness](tailscale.md#serve-readiness)), because `--exit-node=<name>` resolves the name in the netmap, and a tailscaled still in `NoState` has none. A refused name fails the unit with the node list command in the message. The option's type is a host name and not an address because `egress-status` matches by name.

`--exit-node-allow-lan-access` is needed. The kill switch lets replies out ("ct direction reply"), but the packet still has to be routed to the Lima interface first. An exit node puts `default dev tailscale0` into tailscale's routing table 52, and rule 5270 consults that table before `main`. The flag adds throw routes for the local subnets in table 52, which make the lookup fall through to `main` for 192.168.5.0/24. Without it the replies to `yo ssh` would be routed into the tunnel. Observed on a live box: `ip route show table 52` lists `throw 192.168.5.0/24` beside `default dev tailscale0`, and `ip route get 192.168.5.2` answers `dev enp0s1`. The throw routes open nothing: a packet the box starts toward the Lima subnet still meets the kill switch on enp0s1 and is rejected (`connect: No route to host`). Only the box's own subnets are thrown, so the Mac's LAN is routed into the tunnel like any other address, and the exit node decides what it reaches.

With `exitNode = null` the unit does not exist, so a prior choice stays in tailscaled's prefs until someone runs `tailscale set --exit-node=`.

## The tailnet policy

This is a prerequisite of opting in. Selecting the exit node is not enough. The tailnet's access policy must also allow this node to reach the internet through it, which is the `autogroup:internet` destination in a Tailscale ACL or grant. Without that, the exit node accepts the box's packets and drops them.

The policy is also what keeps the box away from the Mac on the tailnet. The kill switch accepts everything that leaves by `tailscale0`, whatever its destination, so if the Mac is a node of the same tailnet, the box reaches it as far as the policy allows. Grant this node `autogroup:internet` and its resolver, and no access to the Mac's node. Separately, tailscaled may send its WireGuard packets straight to a peer's LAN address when it finds one, the Mac's included. Those are tunnel packets, encrypted end to end and answered only by tailscaled, not a channel a process in the box can use.

Recognise it by a split: names resolve (the resolver is on the tailnet), `tailscale ping my-exit-node` answers, but every HTTPS request times out. `yolobox-guest egress-status` says BLOCKED, with the exit node selected and online and a hint to check that the policy grants this node `autogroup:internet` via the exit node. `yo status` shows the same line as `egress:`. The fix is in the policy, not in the box.

## Recognising failures

- `egress: BLOCKED ... none selected`: the exit node is configured but tailscaled has not selected it. Run the command the line names, or restart `yolobox-tailscale-exit-node`.
- `egress: BLOCKED ... selected but offline`, or `no node named ... in the tailnet`: the name in `local.nix` is wrong, or the node is down or not approved as an exit node.
- `egress: BLOCKED ... traffic does not get through`: the configuration is right and the probe fails. See [The tailnet policy](#the-tailnet-policy).
- `kill switch not armed` (exit 2): `network.json` exists and the table does not. Reboot, or `sudo systemctl start yolobox-killswitch`.
- Nothing works and `yo ssh` fails: the rule file did not load, so dhcpcd refused to start (see [Failing closed](#failing-closed)). Use Lima's serial console.
- `journalctl -k` shows `yolobox-killswitch: direct egress blocked:` lines: a program tried to leave without the tailnet. The line's destination says which.

## VPNs inside the guest

A VPN running in the guest is not supported: it would send its packets out of the Lima interface, which the table rejects. Put the VPN at the exit node instead.

## Rehearsing

Nothing here needs a running box. In a throwaway namespace as root:

```sh
unshare -n sh -c 'ip link set lo up; nft -c -f RULES && nft -f RULES && nft list ruleset'
```

`nft -c` checks syntax. Loading it twice shows the atomic replace. A dummy interface named `enp0s1` with a default route shows `ping` rejected, `ping -m 524288` (mark 0x80000) counted on the accept, and a second dummy `tailscale0` accepted. The nix build cannot check the rule file, because nft needs netlink and the build sandbox forbids it; CI runs `nft --check` on the rendered file instead.
