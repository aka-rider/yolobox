# The Mac's network, as the guest sees it

The guest reaches the outside world through the Mac, so the Mac's network settings decide what the guest can do. This file covers the two VPN shapes that break guest traffic, a DNS filter that breaks one AWS endpoint, and the port 80 and 443 binds lima makes on the Mac.

## VPN shapes

lima NATs the guest's traffic out through the Mac's default route. A VPN on the Mac can therefore break the guest while the Mac itself stays fine, and the fault looks like a guest-only fault. Two shapes have been met so far. They look opposite, and neither raises an error on its own.

### WireGuard owns the default route

This happens with a full-tunnel profile in WireGuard.app. The tunnel does not carry the forwarded packets, so they are blackholed. From the guest, every destination fails on every port: GitHub on 443 and 80, 1.1.1.1 on 53, and even the LAN router. The one exception is `192.168.5.2:53`, lima's own resolver, which keeps answering because the host process serves it. `nixos-rebuild` fails with GitHub fetch timeouts. Disconnecting the tunnel restores egress at once.

### ProtonVPN connected

General guest egress keeps working, and so does `ssh git@github.com`. But every guest connection to `192.168.5.2:<port>` is silently blackholed. That address is gvproxy's NAT of lima's gateway address to the Mac's own loopback. A forwarded TCP port relies on exactly that path. That is why the AWS broker reaches the guest over a reverse unix-socket forward instead (see [Transport](aws.md#transport)).

### With the kill switch armed

This section applies only to a box that opted in to the [kill switch](../nix/killswitch.md#opting-in). Without it the two shapes above describe the box as ever.

Such a box has one way out: its tailnet exit node. The kill switch rejects every packet that leaves by any interface but the tailnet, except tailscaled's own packets, which carry fwmark `0x80000`, and even those are rejected when they are addressed to the Lima link itself (its port mapper asks the gateway, which is the Mac). So a Mac VPN matters only to tailscaled. Its control-plane and exit-node packets still leave through the Mac's default route, and a VPN that blackholes them takes the tailnet down while the Mac itself stays fine. General guest egress does not leave by the Mac, so the two shapes above do not describe it. Check `egress:` in `yo status` first. It names the desired exit node and what is actually selected.

The gateway probe is rejected by design on such a box. `yolobox-guest net-check` therefore reports `gateway=skipped` whenever `/etc/yolobox/network.json` exists (it exists only on a box that opted in), and `network:` defers to the `egress:` line. A failed gateway probe on such a box is expected, and it says nothing about a VPN.

### A VPN that disconnects leaves UDP flows stuck

lima's vz networking is gvproxy inside the host agent. For each guest UDP flow it opens a socket on the Mac, and the socket is bound to whichever address the Mac's default route had then. tailscaled keeps sending on the same flows (UDP 41641 to its peers' endpoints, and STUN to the DERP server), so they never go idle. If the VPN that owned the default route disconnects, those sockets keep the VPN's tunnel address and every datagram fails.

Seen live: the Mac had a VPN up at host agent start (tailscaled's first `endpoints changed` line then listed a STUN address that was not the Mac's own public one, and the box was direct to two peers) and dropped it later. From that second on, `~/.lima/yolobox/ha.stderr.log` gained 2,136 lines in 45 minutes of the form

```
{"level":"error","msg":"Can't proxy a datagram to udp: write udp 10.2.0.2:51145->203.0.113.7:41641: write: can't assign requested address\n",...}
```

and `lsof -nP -a -p $(cat ~/.lima/yolobox/ha.pid) -iUDP` listed sockets whose local address was that vanished tunnel address (`10.2.0.2`), which `ifconfig` no longer showed. Inside the guest, tailscaled logged `netcheck: UDP is blocked, trying HTTPS` every 25 seconds starting the same second, while `tailscale netcheck` on the command line, whose fresh ephemeral sockets make fresh flows, reported `UDP: true`. The first symptom for the operator is tailscaled's relayed-contact lines ([nix/tailscale.md](../nix/tailscale.md#relayed-contacts-in-the-log)) and peers that stay on DERP.

The box stays reachable: DERP and the exit node's TCP paths are not affected. Only direct UDP paths and the endpoint discovery behind them are. The cure that ends the stale flows is a host agent restart, `limactl stop yolobox && yo up`, which drops every flow. That restart is the expected cure and has not been run against this failure yet.

### Telling them apart

Recognise the two by those patterns, and run `scutil --nc list` on the Mac before debugging anything inside the VM.

`yo status` tells them apart on its `network:` line. It opens a temporary TCP listener on the Mac's `127.0.0.1:<random port>`, then runs `yolobox-guest net-check <port>` as the operator. That prints two results:

- `egress=`, an HTTPS fetch of github.com.
- `gateway=`, a TCP connect to `192.168.5.2:<port>` through bash `/dev/tcp`, which is the NAT path above. A pass means gvproxy really reaches the Mac's loopback. `skipped` on a box that opted in to the kill switch, see above.

`network_status_line()` maps the pair. Egress dead with gateway ok is the WireGuard shape. Egress ok with gateway dead is the ProtonVPN shape. Both dead means the guest network itself is down, and no VPN caused that. A box whose `yolobox-guest` predates `net-check` prints `network: not checked` with the reason, and the rest of `yo status` still runs.

## The DNS sinkhole

The first launch of v1, the Docker-based predecessor, showed a DNS failure that applies to a guest that resolves through lima, as boxes did before the kill switch. Credentials arrived, but `aws sts get-caller-identity` died with `Could not connect to the endpoint URL: "https://sts.amazonaws.com/"`.

That error is DNS, not AWS. This Mac's DNS filter sinkholes the global `sts.amazonaws.com` name to 0.0.0.0. A guest that uses lima's resolver (192.168.5.2) inherits the Mac's resolver and with it the filter. The AWS CLI targets the global endpoint only when the profile has no region. Regional endpoints such as `sts.eu-west-1.amazonaws.com` resolve fine on both sides.

That is why a profile without a region is left out of the [allowlist](aws.md#allowlist) rather than shipped broken.

A box that opted in to the kill switch no longer asks lima's resolver anything. Its `networking.nameservers` names a resolver on the tailnet, so the Mac's filter does not apply to it and `sts.amazonaws.com` answers with a real address. The rule against profiles without a region stays, because it also keeps the region from drifting out from under the account. See [DNS](../nix/killswitch.md#dns) for why the box must never send DNS to the Lima gateway.

## lima on port 80 and 443

On macOS, a non-root process cannot bind `127.0.0.1:80`, but it can bind `0.0.0.0:80`. So lima binds `0.0.0.0` for any host port below 1024. It wraps that listener so it drops any non-loopback peer after the handshake. This is not exposure.

The real cost is that nothing else on the Mac can bind port 80 or 443.
