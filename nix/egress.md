# The egress table

The code is `nix/egress.nix`. It is always on. The box reaches the internet through Lima's NAT, and the table rejects every guest-originated packet that is addressed to the host or to a private network. The promise is that the guest firewall controls every entrance from the guest into the host's private and Lima addresses: DNS to Lima's gateway on a plain box and DHCP are the only ones it leaves open. The host's public address, reached through the same NAT, is not an address the firewall can tell from the internet (see [Residuals](#residuals)). The reverse unix sockets for 1Password and the AWS broker are ssh channels the guest firewall never sees. One more entrance is not the firewall's to control: the tailnet, whose access policy decides (see [The tailnet is an entrance](#the-tailnet-is-an-entrance)).

## What is blocked and why

Lima's gateway, 192.168.5.2, is gvproxy's NAT to the host's own loopback, on every port (`natTable()` in lima's `pkg/networks/usernet/gvproxy.go`). A guest that connects to 192.168.5.2:5432 reaches whatever listens on the host's `127.0.0.1:5432`, a database or a dev server the user never meant to share. A guest connection to the host's LAN address, or to any host on the LAN, also leaves through gvproxy. So the table rejects, with `reject with icmpx type admin-prohibited` (a blocked program fails at once instead of timing out):

- IPv4: `10.0.0.0/8`, `100.64.0.0/10`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16` (which holds the Lima subnet), `224.0.0.0/4` and `255.255.255.255`;
- IPv6: `fc00::/7`, `fe80::/10` and `ff00::/8`.

These are accepted before the rejects are judged:

- anything that leaves by `lo`, `tailscale0` or an interface in `yolobox.network.tunnelInterfaces`. The tailnet carries CGNAT and `fd7a:115c:a1e0::/48` addresses, so this rule must come first. It also means the table never judges tailnet traffic, see [The tailnet is an entrance](#the-tailnet-is-an-entrance), and likewise for a tunnel interface, see [`tunnelInterfaces`](#tunnelinterfaces);
- replies to flows the outside started (`ct direction reply`), which keeps Lima's inbound ssh to 192.168.5.15:22 working;
- DHCP (`udp sport 68 udp dport 67`), and only to `255.255.255.255` and the gateway. Port 68 is not privileged for the agent (`net.ipv4.ip_unprivileged_port_start=0` comes from nixos-lima), so a rule that named no destination would let it send UDP to port 67 of any host;
- DNS, UDP and TCP port 53, to 192.168.5.2 and nothing else on it. This is the one exception on a plain box. DNS goes through the gateway because the lease names it as the resolver;
- IPv6 neighbour discovery and MLD reports to link-local groups (`ff02::/16` keeps the accept from passing a report off the link);
- the networks in `yolobox.network.lanAllow`.

The table has an output chain only, and a postrouting chain when `tunnelInterfaces` is set (see [`tunnelInterfaces`](#tunnelinterfaces)). The box does not route for others, so there is no forward chain.

The priority is -20. A packet must pass every base chain on the output hook, and a reject in either of two tables is final, so this table composes with a kill switch module that adds its own table without either one knowing about the other. With equal priorities the kernel orders chains by load order, which is not fixed, so the egress table sits at -20, strictly before a kill switch at -10. It still runs after mangle (-150) and dstnat (-100), so the destination it judges is the final one. The interface is not, see [The output hook's interface](#the-output-hooks-interface).

## The output hook's interface

`oifname` in an output chain is the interface of the route the packet took before the hook ran, and it stays that for every chain on the hook, whatever a NAT does. The kernel runs the output hook from `__ip_local_out()` with `out = skb_dst(skb)->dev` and stores it in the hook state that every chain shares; nft's `meta oif` and `oifname` read that state (`nft_out(pkt)`, `pkt->state->out`). The NAT hook at -100 rewrites the destination and calls `ip_route_me_harder()`, which gives the packet a new route but leaves the hook state alone. So a chain at -20 sees the new destination with the old interface. Postrouting is different: `ip_output()` runs it with the device of the route the packet really has, so `oifname` there is the interface it leaves by. `fib daddr oifname` cannot stand in, because the kernel allows that result only in prerouting, input and forward, and `reject` is not allowed in postrouting, so a postrouting chain drops.

A plain box rewrites nothing, so its output chain judges true interfaces. A module that DNATs does not, and that broke [`tunnelInterfaces`](#tunnelinterfaces) as first built.

## A kill switch module on top

A box that loads a separate kill switch module, such as one that rejects everything not leaving through a tailnet exit node, rejects DNS to the gateway too: the kill switch table judges the packet after this one has accepted it, and its reject is final. Such a module creates `/etc/yolobox/gateway-blocked`, and `net-check` then skips its gateway probe (see [yolobox-guest](guest/yolobox-guest.md)). This table does not know about it and does not need to.

## `lanAllow`

`yolobox.network.lanAllow` is a list of CIDR strings, empty by default. An empty list emits no rule at all, because an empty nft set is a syntax error. A box that needs a LAN service, say a package cache, names its network in `/etc/yolobox/local.nix`:

```nix
{ ... }: {
  yolobox.network.lanAllow = [ "192.168.1.20/32" ];
}
```

Name single addresses where you can. An entry that covers the host's own LAN address, such as the whole `192.168.1.0/24` when the host sits on it, lets the guest reach the host at that address, and the table then protects nothing.

IPv4 entries become one `ip daddr { … } accept` and IPv6 entries one `ip6 daddr { … } accept`, placed before the rejects. An entry that is not a CIDR fails the evaluation with `yolobox.network.lanAllow: not a CIDR: <entries>`, because the string goes into the rule file and a typo must not reach `nft` on a user's box, where it would drop the box into recovery mode. IPv4 octets must be 0 to 255 without leading zeros and the prefix 0 to 32. IPv6 entries need well-formed groups (at most eight, `::` once) and a prefix of 0 to 128. CI evaluates `300.1.1.1/8`, `10.0.0.0/33`, `lizard` and `fd00::/129` and expects each to fail.

An entry that covers the Lima gateway (`192.168.5.2/32`, `192.168.5.0/24`, `192.168.0.0/16`), or `0.0.0.0/0` or `::/0`, cancels the protection: it is accepted before the rejects, so the guest again reaches every port of the host's loopback through the gateway, or every address at all. The evaluation accepts such entries on purpose. Only the root-owned `local.nix` can set the option, so this is the operator's explicit choice, and the table does not second-guess it.

## `tunnelInterfaces`

`yolobox.network.tunnelInterfaces` is a list of interface names, empty by default. Each one joins `lo` and `tailscale0` in the first rule, so with `[ "wg0" ]` the rule renders as `oifname { "lo", "tailscale0", "wg0" } accept`. With the default, the rendered file is byte-identical to one without the option. Traffic out of a listed interface is not judged by this table, exactly like `tailscale0`: the tunnel, and whatever module owns it, is the gate for what goes through.

It exists for a private egress module, a separate nft table that makes a tunnel (WireGuard, Tor or similar) the box's only way out. Such a module redirects DNS to a resolver inside the tunnel, and that resolver sits in a private range. The redirect happens at dstnat (-100), before this table's hook at -20, so the packet this table sees is addressed to the private resolver, and the blocked-destination rejects would refuse it on any interface.

The first version (279f822) only added the interface to the first rule, and that missed the packets it was for. The box's resolver is the Lima gateway, so a query starts on the route to `192.168.5.2`, out of `enp0s1`. The module DNATs it to `10.64.0.1`, the kernel reroutes it into `wg0`, and this table, still seeing `enp0s1` (see [The output hook's interface](#the-output-hooks-interface)), rejected it as an address in `10.0.0.0/8`. Only a query that was routed into `wg0` before the rewrite, one to `8.8.8.8` say, got through. A test in a network namespace with the module's routing showed both: the gateway query counted on the `10.0.0.0/8` reject, the `8.8.8.8` query on the `wg0` accept.

So with tunnel interfaces the file has two more pieces. In the output chain, `ct status dnat accept` follows the replies: a DNATed packet is not judged there, because its interface is not known yet. A postrouting chain at -20 judges it instead, where the interface is the real one: `ct status dnat oifname { "lo", "tailscale0", "wg0" } accept`, then the same blocked ranges, logged and dropped. The gateway's DNS, DHCP and `lanAllow` are not accepted there: a rewritten packet that does not leave by an accepted interface may not reach any private address. The interface check matters on its own: any program may bind a socket to `enp0s1` (`SO_BINDTODEVICE` needs no privilege on a socket not yet bound), and the reroute keeps that interface, so a query DNATed to `10.64.0.1` would leave by `enp0s1` toward the host. The postrouting chain drops it; the namespace test counted it on the `10.0.0.0/8` drop.

A plain box rewrites nothing, so the default file has neither piece and is unchanged.

The module's own table sits at priority -10, after this one at -20. A packet must pass both, and a reject in either is final, so they compose without knowing each other: this table keeps the guest off the host and the LAN, and the private table rejects whatever does not leave by the tunnel.

A name is checked when the box is evaluated: 1 to 15 characters of `A-Za-z0-9_.-` (the kernel's limit is 15), and not a repeat of another entry, `lo` or `tailscale0`. Anything else fails with `yolobox.network.tunnelInterfaces: …`, because the string goes into the rule file and a bad one must not reach `nft`, where it would drop the box into recovery mode. The recovery file never reads the option.

## The tailnet is an entrance

`oifname { "lo", "tailscale0" } accept` is the first rule (with any `tunnelInterfaces` after `tailscale0`), so the table never judges what leaves by `tailscale0`. The interface is the one before any NAT; nothing in the repo rewrites a packet, so on a plain box that is the one it leaves by. If the host is itself a node on the tailnet, which it is when the host runs Tailscale, the guest can connect to the host's tailnet address and reach every host service bound to `0.0.0.0` or to that address. The firewall does not stop it. On a default tailnet policy, which allows everything, nothing does. A box with a kill switch module accepts `tailscale0` too.

That is by design: the tailnet access policy is the gate. Close it there, with a policy that does not let the box's tag or user reach the host's node. Do not expect this table to.

## Residuals

- A fake-IP proxy on the host (Surge, Clash, sing-box or Stash in fake-IP mode) answers every name with an address in `198.18.0.0/15`. The table does not block that range: the box reaches the internet through the proxy, which is the host's own choice, and a block would take the whole box offline.
- A guest connection to the host's public address leaves through the NAT and returns through the host's network. The table cannot tell it from any other internet traffic, so a service the host exposes on its public address stays reachable (the hairpin). The same holds for any host address outside the blocked sets, such as a public IPv4 address assigned directly on the host's LAN interface.
- `AF_VSOCK` to the host is unjudged. The table is an output hook on IP traffic, so netfilter never sees `AF_VSOCK`. A live probe to host CID 2 found no listener: every port tried was answered with a connection reset, because Lima runs with `overVsock:false`. Nothing is reachable today. A future host-side vsock listener would sit entirely outside the table's reach.
- No global IPv6. Lima's NAT gives the guest no global IPv6 address. `enp0s1` has only a link-local `fe80` address, there is no IPv6 default route, and only `tailscale0` carries a ULA. So the IPv6 form of the host-address hairpin is unreachable on a current box. The `fc00::/7`, `fe80::/10` and `ff00::/8` rejects still cover the v6 addresses that exist.
- `127/8` out `enp0s1` relies on the kernel's martian filter. `127.0.0.0/8` is not in the table's blocked set. A packet to `127/8` that leaves a non-loopback interface is dropped by the kernel as a martian, because `route_localnet` is 0, so it never reaches the host's loopback. That protection lives in the kernel, not in the table. A future change to `route_localnet` would re-open it.
- The terminal's OSC 52 clipboard sequence (`PI_CLIPBOARD_OSC52`) carries text from the guest to the host's clipboard. It is not network traffic and out of scope here.
- tailscaled's underlay is not exempt. Its NAT-PMP, PCP and SSDP probes go to 192.168.5.2 and 239.255.255.250, which the table rejects, and `TS_DISABLE_PORTMAPPER=1` in [tailscale.nix](tailscale.nix) stops them being sent. A peer on the host's LAN therefore connects through public endpoints, then DERP, which is slower. See [Tailscale](tailscale.md).

## The unit and recovery mode

The rules load as one atomic nft transaction: `table X`, `delete table X` and `table X { … }` in one file, so a reload swaps the rules with no instant at which the table is missing. The file is `builtins.toFile`, which writes at evaluation time, so CI can `nft --check` it without building anything. It is a separate table, not `networking.nftables`, which would switch the firewall backend and `nixos-fw` stays on iptables-nft.

`yolobox-egress.service` has no `ExecStop`, deliberately: stopping it, or a switch that restarts it, must never open the box. A table with no owner is only ever deleted by a person.

The unit loads two files:

1. `ExecStartPre` loads the recovery table, a static file that names nothing from the options, so no `local.nix` can break it. It accepts `lo`, replies, DHCP and neighbour discovery and rejects everything else, so the box has no network the guest starts, and Lima's ssh still works.
2. `ExecStart` loads the full rules, which replace the recovery table in one transaction.

When the full rules fail to load, for example after an `nft` incompatibility, the unit is `failed`, the recovery table stays, and the system is `degraded`. The box never fails open and the operator keeps Lima ssh. Recovery is offline, so a rebuild cannot fetch anything. The way out is to roll back to the previous generation, which is already built:

```sh
yo ssh sudo nixos-rebuild switch --rollback
```

Then fix `local.nix` and rebuild. `yo status` shows `egress: WARNING recovery mode …` with the unit's state, and `egress: host and LAN blocked` when the unit is active. A box built before the unit existed has no such unit at all (`LoadState=not-found`), and `yo status` says `egress: box predates the egress table; run yo bootstrap` instead of calling it recovery mode.

Between the two loads, on a restart of the unit during a switch, the recovery table is in force for a moment. Guest-initiated packets sent in that instant are rejected.

`tailscaled` is gated on the table: `ExecStartPre = nft list table inet yolobox-egress` refuses to start it when the table is absent, and the list merges with any other module's gate. `dhcpcd` is ordered after the unit but never gated, because without an address there is no Lima ssh and no way back in.

## Recognising it

- `sudo nft list table inet yolobox-egress` shows the table. In recovery mode its last rule is a reject with no `ip daddr` match.
- `journalctl -k | grep 'yolobox-egress: blocked:'` lists rejected packets, rate-limited to 6 a minute with a burst of 3.
- `journalctl -k | grep 'yolobox-egress: recovery:'` lists the packets rejected in recovery mode.
- `systemctl is-active yolobox-egress` prints `active` only when the full rules are loaded.

## How the rules are tested

The tests read the rendered rule files, not the Nix, because the rendered text is what the kernel runs. `tests/egress_rules.py` parses a file line by line and fails on the first missing property: the table and hook priority, the first rule (`lo` and `tailscale0`, and in the tunnel file the extra interface names, `ct status dnat accept` and a postrouting chain that judges rewritten packets by the same interfaces and ranges), DHCP only to the broadcast and the gateway, DNS as the only other thing the gateway gets, every blocked range rejected after the accepts, and a recovery file that accepts only `lo`, replies, DHCP and neighbour discovery and then rejects. `tests/test_egress_rules.py` runs it on copies of the rendered files in `tests/fixtures/` and on broken variants of them that must fail.

The host has no nix, so those copies are made in the guest: `nix eval --impure --raw .#nixosConfigurations.yolobox.config.system.build.egressRules` (and `egressRecoveryRules`) prints a store path, and the file at that path replaces `tests/fixtures/egress.nft` (and `egress-recovery.nft`); `tests/render-tunnel-egress.sh` prints the one for `tests/fixtures/egress-tunnel.nft`, rendered with `tunnelInterfaces = [ "wg0" ]`. In a box whose `/etc/yolobox/local.nix` changes the network options, render in a mount namespace that hides it (`sudo unshare -m sh -c 'mount -t tmpfs none /etc/yolobox; …'`), because `base.nix` imports it. CI renders the same three files from the commit, runs the checker on them and diffs them against the copies, so a copy cannot drift from `egress.nix`. That job fails after any edit of the rules until the copies are refreshed. The unit's own properties (no `ExecStop`, `tailscaled` gated, `dhcpcd` not) are not in a rule file and stay source checks in `tests/test_consistency.py`.
