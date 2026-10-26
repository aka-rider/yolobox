# The egress table

The code is `nix/egress.nix`. It is always on. The box reaches the internet through Lima's NAT, and the table rejects every guest-originated packet that is addressed to the host or to a private network. The promise is that the guest firewall controls every entrance from the guest into the host that goes through Lima's NAT: DNS to Lima's gateway on a plain box and DHCP. The reverse unix sockets for 1Password and the AWS broker are ssh channels the guest firewall never sees. One more entrance is not the firewall's to control: the tailnet, whose access policy decides (see [The tailnet is an entrance](#the-tailnet-is-an-entrance)).

## What is blocked and why

Lima's gateway, 192.168.5.2, is gvproxy's NAT to the host's own loopback, on every port (`natTable()` in lima's `pkg/networks/usernet/gvproxy.go`). A guest that connects to 192.168.5.2:5432 reaches whatever listens on the host's `127.0.0.1:5432`, a database or a dev server the user never meant to share. A guest connection to the host's LAN address, or to any host on the LAN, also leaves through gvproxy. So the table rejects, with `reject with icmpx type admin-prohibited` (a blocked program fails at once instead of timing out):

- IPv4: `10.0.0.0/8`, `100.64.0.0/10`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16` (which holds the Lima subnet), `198.18.0.0/15` (the benchmarking range, where a fake-IP proxy on the host answers), `224.0.0.0/4` and `255.255.255.255`;
- IPv6: `fc00::/7`, `fe80::/10` and `ff00::/8`.

These are accepted before the rejects are judged:

- anything that leaves by `lo` or `tailscale0`. The tailnet carries CGNAT and `fd7a:115c:a1e0::/48` addresses, so this rule must come first. It also means the table never judges tailnet traffic, see [The tailnet is an entrance](#the-tailnet-is-an-entrance);
- replies to flows the outside started (`ct direction reply`), which keeps Lima's inbound ssh to 192.168.5.15:22 working;
- DHCP (`udp sport 68 udp dport 67`), and only to `255.255.255.255` and the gateway. Port 68 is not privileged for the agent (`net.ipv4.ip_unprivileged_port_start=0` comes from nixos-lima), so a rule that named no destination would let it send UDP to port 67 of any host;
- DNS, UDP and TCP port 53, to 192.168.5.2 and nothing else on it. This is the one exception on a plain box. DNS goes through the gateway because the lease names it as the resolver;
- IPv6 neighbour discovery and MLD reports to link-local groups (`ff02::/16` keeps the accept from passing a report off the link);
- the networks in `yolobox.network.lanAllow`.

The table has an output chain only. The box does not route for others, so there is no forward chain.

The priority is -20. A packet must pass every base chain on the output hook, and a reject in either of two tables is final, so this table composes with a kill switch module that adds its own table without either one knowing about the other. With equal priorities the kernel orders chains by load order, which is not fixed, so the egress table sits at -20, strictly before a kill switch at -10. It still runs after mangle (-150) and dstnat (-100), so the interface it judges is the final one.

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

Allowing `192.168.5.0/24`, the Lima subnet, would open the host again through the gateway. Do not.

## The tailnet is an entrance

`oifname { "lo", "tailscale0" } accept` is the first rule, so the table never judges what leaves by `tailscale0`. If the host is itself a node on the tailnet, which it is when the host runs Tailscale, the guest can connect to the host's tailnet address and reach every host service bound to `0.0.0.0` or to that address. The firewall does not stop it. On a default tailnet policy, which allows everything, nothing does. A box with a kill switch module accepts `tailscale0` too.

That is by design: the tailnet access policy is the gate. Close it there, with a policy that does not let the box's tag or user reach the host's node. Do not expect this table to.

## Residuals

- A guest connection to the host's public address leaves through the NAT and returns through the host's network. The table cannot tell it from any other internet traffic, so a service the host exposes on its public address stays reachable (the hairpin). The same holds for any host address outside the blocked sets, such as a public IPv4 address assigned directly on the host's LAN interface.
- `AF_VSOCK` to the host (CID 2) is not seen by netfilter. Whether Lima's vz driver registers a host-side vsock listener a guest could dial has not been checked, see `TODO.md`.
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
