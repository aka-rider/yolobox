# The egress table

The code is `nix/egress.nix`. It is always on. The box reaches the internet through Lima's NAT, and the table rejects every guest-originated packet that is addressed to the host or to a private network. The promise is that the guest firewall controls every entrance from the guest into the host's private and Lima addresses: DNS to Lima's gateway on a plain box and DHCP are the only ones it leaves open. The host's public address, reached through the same NAT, is not an address the firewall can tell from the internet (see [Residuals](#residuals)). The reverse unix sockets for 1Password and the AWS broker are ssh channels the guest firewall never sees. One more entrance is not the firewall's to control: the tailnet, whose access policy decides (see [The tailnet is an entrance](#the-tailnet-is-an-entrance)).

## What is blocked and why

Lima's gateway, 192.168.5.2, is gvproxy's NAT to the host's own loopback, on every port (`natTable()` in lima's `pkg/networks/usernet/gvproxy.go`). A guest that connects to 192.168.5.2:5432 reaches whatever listens on the host's `127.0.0.1:5432`, a database or a dev server the user never meant to share. A guest connection to the host's LAN address, or to any host on the LAN, also leaves through gvproxy. So the table rejects, with `reject with icmpx type admin-prohibited` (a blocked program fails at once instead of timing out):

- IPv4: `10.0.0.0/8`, `100.64.0.0/10`, `169.254.0.0/16`, `172.16.0.0/12`, `192.0.0.0/24` (the per-host protocol assignments, among them the CLAT and DS-Lite addresses), `192.168.0.0/16` (which holds the Lima subnet), `198.18.0.0/15` (see [A fake-IP proxy](#a-fake-ip-proxy-on-the-host)), `224.0.0.0/4` and `240.0.0.0/4` (reserved, and the limited broadcast `255.255.255.255` inside it);
- IPv6: `fc00::/7`, `fe80::/10`, `ff00::/8` and `64:ff9b:1::/48`.

These are accepted before the rejects are judged:

- anything that leaves by `lo`, `tailscale0` or an interface in `yolobox.network.tunnelInterfaces`. The tailnet carries CGNAT and `fd7a:115c:a1e0::/48` addresses, so this rule must come first. It also means the table never judges tailnet traffic, see [The tailnet is an entrance](#the-tailnet-is-an-entrance), and likewise for a tunnel interface, see [`tunnelInterfaces`](#tunnelinterfaces);
- replies to flows the outside started (`ct direction reply`), which keeps Lima's inbound ssh to 192.168.5.15:22 working;
- DHCP (`udp sport 68 udp dport 67`), and only to `255.255.255.255` and the gateway. Port 68 is privileged for the agent (`nix/hardening.nix` forces `net.ipv4.ip_unprivileged_port_start` to 1024 over nixos-lima's 0, see [nix/hardening.md](hardening.md#sysctls)), yet the rule still names its destinations, so it does not depend on that sysctl: without them it would let any process that holds port 68 send UDP to port 67 of any host;
- DNS, UDP and TCP port 53, to 192.168.5.2 and nothing else on it. This is the one exception on a plain box. DNS goes through the gateway because the lease names it as the resolver;
- IPv6 neighbour discovery and MLD reports to link-local groups (`ff02::/16` keeps the accept from passing a report off the link);
- the networks in `yolobox.network.lanAllow`.

The table has an output chain only, and a postrouting chain when `tunnelInterfaces` is set (see [`tunnelInterfaces`](#tunnelinterfaces)). The box does not route for others, so there is no forward chain.

The priority is -20. A packet must pass every base chain on the output hook, and a reject in either of two tables is final, so this table composes with another module that adds its own table (a kill switch, say) without either one knowing about the other. With equal priorities the kernel orders chains by load order, which is not fixed, so the egress table sits at -20, strictly before a module's table at -10. It still runs after mangle (-150) and dstnat (-100), so the destination it judges is the final one. The interface is not, see [The output hook's interface](#the-output-hooks-interface).

`64:ff9b:1::/48` is the NAT64 local-use prefix (RFC 8215). A network-specific NAT64 may map private IPv4 addresses into it, so an address in it can be a host on the LAN. The well-known prefix `64:ff9b::/96` is deliberately not blocked: RFC 6052 forbids translating private IPv4 addresses with it, and a host that has only IPv6 needs it to reach the IPv4 internet.

DHCPv6 is not among the accepts: its servers are `ff02::1:2`, inside the rejected `ff00::/8`. dhcpcd used to try it anyway and logged `dhcp6_sendmessage: enp0s1: sendmsg: Operation not permitted` (and `failed to confirm prior DHCPv6 address`) at error level on every boot, so `nix/egress.nix` sets `nodhcp6`. The box's IPv6 address comes from router advertisements (SLAAC), which neighbour discovery carries: a Mac box gets only its link-local address, and a Linux host's passt advertises a prefix, so `ip -6 addr show enp0s1` shows a `scope global ... mngtmpaddr` address there.

## The output hook's interface

`oifname` in an output chain is the interface of the route the packet took before the hook ran, and it stays that for every chain on the hook, whatever a NAT does. The kernel runs the output hook from `__ip_local_out()` with `out = skb_dst(skb)->dev` and stores it in the hook state that every chain shares; nft's `meta oif` and `oifname` read that state (`nft_out(pkt)`, `pkt->state->out`). The NAT hook at -100 rewrites the destination and calls `ip_route_me_harder()`, which gives the packet a new route but leaves the hook state alone. So a chain at -20 sees the new destination with the old interface. Postrouting is different: `ip_output()` runs it with the device of the route the packet really has, so `oifname` there is the interface it leaves by. `fib daddr oifname` cannot stand in, because the kernel allows that result only in prerouting, input and forward, and `reject` is not allowed in postrouting, so a postrouting chain drops.

A plain box rewrites nothing, so its output chain judges true interfaces. A module that DNATs does not, which is why [`tunnelInterfaces`](#tunnelinterfaces) needs more than one rule.

## Another table on top

A box whose `local.nix` adds a separate table that rejects everything not leaving through a tailnet exit node (a kill switch) rejects DNS to the gateway too: that table judges the packet after this one has accepted it, and its reject is final. A box that does this creates `/etc/yolobox/gateway-blocked`, and `net-check` then skips its gateway probe (see [yolobox-guest](guest/yolobox-guest.md)). This table does not know about it and does not need to.

## `lanAllow`

`yolobox.network.lanAllow` is a list of CIDR strings, empty by default. An empty list emits no rule at all, because an empty nft set is a syntax error. A box that needs a LAN service, say a package cache, names its network in `/etc/yolobox/local.nix`:

```nix
{ ... }: {
  yolobox.network.lanAllow = [ "192.168.1.20/32" ];
}
```

On a Linux host `lanAllow` does nothing. The guest table accepts the entry, but the packet then leaves through passt, and the host table (`linux/yolobox-host.nft`) rejects the private ranges for passt's uid, with no equivalent of this option. The connection fails with a reject on the host, and the guest's own table shows nothing wrong. A Linux-host box that needs a LAN service has to be served another way (the tailnet, say) until the host table learns the same allowance. See [the host table](../docs/linux-host.md#the-host-table).

Name single addresses where you can. An entry that covers the host's own LAN address, such as the whole `192.168.1.0/24` when the host sits on it, lets the guest reach the host at that address, and the table then protects nothing.

IPv4 entries become one `ip daddr { … } accept` and IPv6 entries one `ip6 daddr { … } accept`, placed before the rejects. An entry that is not a CIDR fails the evaluation with `yolobox.network.lanAllow: not a CIDR: <entries>`, because the string goes into the rule file and a typo must not reach `nft` on a user's box, where it would drop the box into recovery mode. IPv4 octets must be 0 to 255 without leading zeros and the prefix 0 to 32. IPv6 entries need well-formed groups (at most eight, `::` once) and a prefix of 0 to 128. The address must be the network's own address, with no bit set past the prefix: nft loads `192.168.1.20/24` without complaint, as `192.168.1.0/24`, and `fd00::1/64` as `fd00::/64`, so an operator who meant one host would have opened the whole network it lies in. Such an entry fails the evaluation with the same message, and the fix is `/32` (or `/128`) for a host or the network's address for a network. CI evaluates `300.1.1.1/8`, `10.0.0.0/33`, `lizard`, `fd00::/129`, `192.168.1.20/24` and `fd00::1/64` and expects each to fail.

An entry that covers the Lima gateway (`192.168.5.2/32`, `192.168.5.0/24`, `192.168.0.0/16`), or `0.0.0.0/0` or `::/0`, cancels the protection: it is accepted before the rejects, so the guest again reaches every port of the host's loopback through the gateway, or every address at all. The evaluation accepts such entries on purpose. Only the root-owned `local.nix` can set the option, so this is the operator's explicit choice, and the table does not second-guess it.

## `tunnelInterfaces`

`yolobox.network.tunnelInterfaces` is a list of interface names, empty by default. Each one joins `lo` and `tailscale0` in the first rule, so with `[ "wg0" ]` the rule renders as `oifname { "lo", "tailscale0", "wg0" } accept`. With the default, the rendered file is byte-identical to one without the option. Traffic out of a listed interface is not judged by this table, exactly like `tailscale0`: the tunnel, and whatever module owns it, is the gate for what goes through.

It exists for a box whose `local.nix` adds a separate nft table that makes a tunnel (WireGuard, Tor or similar) the box's only way out. Such a table redirects DNS to a resolver inside the tunnel, and that resolver sits in a private range. The redirect happens at dstnat (-100), before this table's hook at -20, so the packet this table sees is addressed to the private resolver, and the blocked-destination rejects would refuse it on any interface.

Adding the interface to the first rule alone misses the packets it is for. The box's resolver is the Lima gateway, so a query starts on the route to `192.168.5.2`, out of `enp0s1`. The tunnel table DNATs it to the tunnel's resolver (say `10.99.0.1`), the kernel reroutes it into `wg0`, and this table, still seeing `enp0s1` (see [The output hook's interface](#the-output-hooks-interface)), rejects it as an address in `10.0.0.0/8`. Only a query that was routed into `wg0` before the rewrite, one to `8.8.8.8` say, gets through.

So with tunnel interfaces the file has two more pieces. In the output chain, `ct status dnat accept` follows the replies: a DNATed packet is not judged there, because its interface is not known yet. A postrouting chain at -20 judges it instead, where the interface is the real one: `ct status dnat oifname { "lo", "tailscale0", "wg0" } accept`, then the same blocked ranges, logged and dropped. The gateway's DNS, DHCP and `lanAllow` are not accepted there: a rewritten packet that does not leave by an accepted interface may not reach any private address. The interface check matters on its own: any program may bind a socket to `enp0s1` (`SO_BINDTODEVICE` needs no privilege on a socket not yet bound), and the reroute keeps that interface, so a query DNATed to `10.99.0.1` would leave by `enp0s1` toward the host. The postrouting chain drops it.

A plain box rewrites nothing, so the default file has neither piece and is unchanged.

A tunnel table of this kind sits at priority -10, after this one at -20. A packet must pass both, and a reject in either is final, so they compose without knowing each other: this table keeps the guest off the host and the LAN, and the tunnel table rejects whatever does not leave by the tunnel.

A name is checked when the box is evaluated: 1 to 15 characters of `A-Za-z0-9_.-` (the kernel's limit is 15), and not a repeat of another entry, `lo` or `tailscale0`. Anything else fails with `yolobox.network.tunnelInterfaces: …`, because the string goes into the rule file and a bad one must not reach `nft`, where it would drop the box into recovery mode. The recovery file never reads the option.

## The tailnet is an entrance

`oifname { "lo", "tailscale0" } accept` is the first rule (with any `tunnelInterfaces` after `tailscale0`), so the table never judges what leaves by `tailscale0`. The interface is the one before any NAT; nothing in the repo rewrites a packet, so on a plain box that is the one it leaves by. If the host is itself a node on the tailnet, which it is when the host runs Tailscale, the guest can connect to the host's tailnet address and reach every host service bound to `0.0.0.0` or to that address. The firewall does not stop it. On a default tailnet policy, which allows everything, nothing does.

That is by design: the tailnet access policy is the gate. Close it there, with a policy that does not let the box's tag or user reach the host's node. Do not expect this table to.

## A fake-IP proxy on the host

A fake-IP proxy (Clash, Surge, mihomo and their kin) answers every name with an address in `198.18.0.0/15` and connects on the guest's behalf when the guest connects to that address. Every name: a LAN hostname or the host's own name gets a fake address too, and the proxy then connects to the private address behind it. So through that range the guest reaches any LAN host, and the host itself, by name, and this table never sees a private address. That is why `198.18.0.0/15` is in the blocked set, although an earlier version left it out to keep such a box online.

A box on a Mac behind such a proxy has no internet until its operator opts in, with `yolobox.network.lanAllow = [ "198.18.0.0/15" ]` in `local.nix`, which re-opens the relay with the consequence above. On a Linux host the host table blocks the range for the guest's uid as well, and `lanAllow` does nothing there, so a fake-IP proxy on a Linux host and a box do not mix. Entries in `lanAllow` are the operator's explicit choice, see [`lanAllow`](#lanallow).

## Residuals

- A guest connection to the host's public address leaves through the NAT and returns through the host's network. The table cannot tell it from any other internet traffic, so a service the host exposes on its public address stays reachable (the hairpin). The same holds for any host address outside the blocked sets, such as a public IPv4 address assigned directly on the host's LAN interface.
- `AF_VSOCK` to the host is unjudged. The table is an output hook on IP traffic, so netfilter never sees `AF_VSOCK`. Lima runs with `overVsock:false`, so no host-side listener exists and a connect to host CID 2 is answered with a connection reset. Nothing is reachable today. A future host-side vsock listener would sit entirely outside the table's reach.
- No global IPv6. Lima's NAT gives the guest no global IPv6 address. `enp0s1` has only a link-local `fe80` address, there is no IPv6 default route, and only `tailscale0` carries a ULA. So the IPv6 form of the host-address hairpin is unreachable on a current box. The `fc00::/7`, `fe80::/10` and `ff00::/8` rejects still cover the v6 addresses that exist.
- `127/8` out `enp0s1` relies on the kernel's martian filter. `127.0.0.0/8` is not in the table's blocked set. A packet to `127/8` that leaves a non-loopback interface is dropped by the kernel as a martian, because `route_localnet` is 0, so it never reaches the host's loopback. That protection lives in the kernel, not in the table. A future change to `route_localnet` would re-open it.
- The terminal's OSC 52 clipboard sequence (`PI_CLIPBOARD_OSC52`) carries text from the guest to the host's clipboard. It is not network traffic and out of scope here.
- tailscaled's underlay is not exempt. Its NAT-PMP, PCP and SSDP probes go to 192.168.5.2 and 239.255.255.250, which the table rejects, and `TS_DISABLE_PORTMAPPER=1` in [tailscale.nix](tailscale.nix) stops them being sent. A peer on the host's LAN therefore connects through public endpoints, then DERP, which is slower. See [Tailscale](tailscale.md).

## The unit and recovery mode

The rules load as one atomic nft transaction: `table X`, `delete table X` and `table X { … }` in one file, so a reload swaps the rules with no instant at which the table is missing. The file is `builtins.toFile`, which writes at evaluation time, so CI can `nft --check` it without building anything. It is a separate table, not `networking.nftables`, which would switch the firewall backend and `nixos-fw` stays on iptables-nft.

`yolobox-egress.service` has no `ExecStop`, deliberately: stopping it, or a switch that restarts it, must never open the box. A table with no owner is only ever deleted by a person.

The unit has three tiers of table, each a file that replaces the one before it in one transaction, and each with a chain name of its own so that a listing says which is loaded:

| Tier | File | Chain | Rules |
|---|---|---|---|
| Full | `egressRules` | `output` | The rules above. |
| Recovery | `egressRecoveryRules` | `output_recovery` | Accepts `lo`, replies, DHCP and neighbour discovery, logs, and rejects everything else. |
| Last resort | `egressLastResortRules` | `output_lastresort` | `policy drop`, and accepts `lo`, `ct state established,related` (packets of flows that are already open) and DHCP. No log, no `limit`, no `icmpx`, no set. |

1. `ExecStartPre` runs `yolobox-egress-load-tiers` (`nix/egress/yolobox-egress-load-tiers.sh`): `nft -f recovery || nft -f lastresort`. The recovery file names nothing from the options, so no `local.nix` can break it. It rejects everything else, so the box has no network the guest starts, and Lima's ssh still works.
2. `ExecStart` loads the full rules, which replace whichever tier is loaded in one transaction.

When the full rules fail to load, for example after an `nft` incompatibility, the unit is `failed`, the recovery table stays, and the system is `degraded`. The box never fails open and the operator keeps Lima ssh. Recovery is offline, so a rebuild cannot fetch anything. The way out is to roll back to the previous generation, which is already built:

```sh
yo ssh sudo nixos-rebuild switch --rollback
```

Then fix `local.nix` and rebuild.

The last-resort tier exists for the case in which `nft` cannot load even the recovery file. The two files share statements (`log`, `limit rate`, `ct direction`, `icmpx`) with the full rules, so a kernel or an `nft` that lacks one of them fails the full rules and the recovery tier, and before this tier that left no table at all: `dhcpcd` is not gated on the unit, so the box came up with the guest able to reach the host and the LAN. The last-resort file uses only `oifname`, `ct state` and a `udp` match, and denies by policy, so there is no reject to get wrong. `ct state` still needs conntrack, so a kernel or an `nft` without `ct` at all fails all three tiers: the unit fails with no table loaded, and `yolobox-egress-failed` reports `NO TABLE IS LOADED`. Its DHCP rule names no destination: the agent cannot bind port 68 (see [nix/hardening.md](hardening.md#sysctls)), and a set is one more thing that can fail to load.

The tier's rule is `ct state established,related`, not the recovery tier's `ct direction reply`, on purpose: `ct direction` is one of the statements whose absence fails the recovery file, so using it here would make this tier fail for the very reason it exists. The cost is that the rule is wider than "replies": a flow the guest opened before the tier loaded (an ordinary outbound connection, say) keeps sending until it ends, which a reply-only rule would cut. No new flow can start, so nothing newly reaches the host or the LAN, and the full rules had already rejected flows to both. That is why the messages say "established and related flows", not "replies". It keeps `table inet yolobox-egress` for the same reason as the others: `tailscaled`'s gate lists exactly that table.

When no tier loads, the script fails, the unit fails, and `OnFailure=yolobox-egress-failed.service` runs `yolobox-egress-failed` (`nix/egress/yolobox-egress-failed.sh`). It lists the table, names the tier in force from its chain name, and logs at the journal's critical level, on the console as well. A table that is missing altogether is written in capitals: `NO TABLE IS LOADED, so the guest can reach the host and the LAN`. That is the line to look for in `journalctl -u yolobox-egress-failed`.

`yo status` shows `egress: host and LAN blocked` when the unit is active and its table holds. When the unit is not active it reads the live table and names the tier by the whole chain header line: `egress: WARNING recovery table …`, `last-resort table …`, or `no table (fail-open) …`, the last meaning no rules are loaded and the box may reach the host and the LAN. Each carries the unit's state, the rollback command and the journal command, and each makes `yo status` exit 1. A plain `chain output {` under an inactive unit is read as a recovery table, because boxes built before the tiers existed named it that way. A box built before the unit existed has no such unit at all (`LoadState=not-found`), and `yo status` says `egress: box predates the egress table; run yo bootstrap` instead of calling it a fallback tier. See [yo.md](../yo.md#the-egress-line).

Between the loads, on a restart of the unit during a switch, the recovery table is in force for a moment. Guest-initiated packets sent in that instant are rejected.

`tailscaled` is gated on the table: `ExecStartPre = nft list table inet yolobox-egress` refuses to start it when the table is absent, and the list merges with any other module's gate. `dhcpcd` is ordered after the unit but never gated, because without an address there is no Lima ssh and no way back in.

## Recognising it

- `sudo nft list table inet yolobox-egress` shows the table. The chain name says which tier is loaded: `output`, `output_recovery` or `output_lastresort`. In recovery mode its last rule is a reject with no `ip daddr` match; the last-resort chain has no reject and `policy drop`.
- `journalctl -k | grep 'yolobox-egress: blocked:'` lists rejected packets, rate-limited to 6 a minute with a burst of 3.
- `journalctl -k | grep 'yolobox-egress: recovery:'` lists the packets rejected in recovery mode. The last-resort tier logs nothing, so a box that has network trouble and prints nothing there may be in it: `journalctl -u yolobox-egress-failed` says.
- `systemctl is-active yolobox-egress` prints `active` only when the full rules are loaded.

## How the rules are tested

The tests read the rendered rule files, not the Nix, because the rendered text is what the kernel runs. `tests/egress_rules.py` parses a file line by line and fails on the first missing property: the table and hook priority, the first rule (`lo` and `tailscale0`, and in the tunnel file the extra interface names, `ct status dnat accept` and a postrouting chain that judges rewritten packets by the same interfaces and ranges), DHCP only to the broadcast and the gateway, DNS as the only other thing the gateway gets, every blocked range rejected after the accepts, a recovery file that accepts only `lo`, replies, DHCP and neighbour discovery and then rejects, and a last-resort file that is exactly `lo`, established flows and DHCP under `policy drop` (`check_lastresort`). It also requires each file to have the chain name of its tier. `tests/test_egress_rules.py` runs it on copies of the rendered files in `tests/fixtures/` and on broken variants of them that must fail, and runs the two helper scripts under bash with a fake `nft`: the recovery load first, the last-resort load after a failure, a failing script when neither loads, and the loud message for each tier and for no table.

The host has no nix, so those copies are made in the guest: `YOLOBOX_USERNAME=$(id -un) YOLOBOX_FLAKE_REF=github:aka-rider/yolobox/ci nix eval --impure --raw .#nixosConfigurations.yolobox.config.system.build.egressRules` (and `egressRecoveryRules`) prints a store path, and the file at that path replaces `tests/fixtures/egress.nft` (and `egressRecoveryRules` for `egress-recovery.nft`, `egressLastResortRules` for `egress-lastresort.nft`); `tests/render-tunnel-egress.sh` prints the one for `tests/fixtures/egress-tunnel.nft`, rendered with `tunnelInterfaces = [ "wg0" ]`. In a box whose `/etc/yolobox/local.nix` changes the network options, render in a mount namespace that hides it (`sudo unshare -m sh -c 'mount -t tmpfs none /etc/yolobox; …'`), because `base.nix` imports it. CI renders the same four files from the commit, runs the checker on them and diffs them against the copies, so a copy cannot drift from `egress.nix`. That job fails after any edit of the rules until the copies are refreshed. The unit's own properties (no `ExecStop`, `tailscaled` gated, `dhcpcd` not) are not in a rule file and stay source checks in `tests/test_consistency.py`.
