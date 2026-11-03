# The host's network, as the guest sees it

The guest reaches the outside world through the host, so the host's network settings decide what the guest can do. This file covers the two VPN shapes that break guest traffic, a fake-IP proxy that does not, the marker for a box that blocks the gateway on purpose and a DNS filter that can break one AWS endpoint.

## VPN shapes

lima NATs the guest's traffic out through the host's default route. A VPN on the host can therefore break the guest while the host itself stays fine, and the fault looks like a guest-only fault. Two shapes have been met so far. They look opposite, and neither raises an error on its own.

### A VPN that owns the host's default route

A full-tunnel VPN profile takes over the host's default route, and the tunnel does not carry the forwarded packets, so they are blackholed. From the guest, every destination fails on every port: GitHub on 443 and 80, and 1.1.1.1 on 53. The one exception is `192.168.5.2:53`, lima's own resolver, which keeps answering because the host process serves it. `nixos-rebuild` fails with GitHub fetch timeouts. Disconnecting the tunnel restores egress at once.

### A VPN that blackholes lima's gateway

General guest egress keeps working, and so does `ssh git@github.com`. But every guest connection to `192.168.5.2:<port>` is silently blackholed. That address is gvproxy's NAT of lima's gateway address to the host's own loopback. A forwarded TCP port relies on exactly that path. That is why the AWS broker reaches the guest over a reverse unix-socket forward instead (see [Transport](aws.md#transport)). The gateway's DNS rides the same NAT, so `yo status` asks it a DNS question (see [Telling them apart](#telling-them-apart)) and reports it when it goes unanswered.

### A box that blocks the gateway on purpose

The two shapes are told apart by probing the gateway's DNS. A box whose own configuration (its `/etc/yolobox/local.nix`, or a module that file imports) rejects traffic to the Lima gateway by design, DNS included, would fail that probe every time, and `yo status` would blame a VPN on the host that is not there. Such a box creates the file `/etc/yolobox/gateway-blocked`. Only its existence counts, never its content. While the file exists, `yolobox-guest net-check` prints `gateway=skipped` and does not probe, and the `network:` line in `yo status` says the probe was skipped because the box blocks it by design, with the verdict on egress alone. This repo creates no such file and names no reason for it.

### A VPN that disconnects leaves UDP flows stuck

A host VPN that disconnects can leave lima's guest UDP flows bound to a tunnel address that no longer exists, so every datagram on them fails while the box stays reachable over DERP and TCP. The first symptom is tailscaled's relayed-contact lines ([nix/tailscale.md](../nix/tailscale.md#relayed-contacts-in-the-log)) and peers that stay on DERP. Restart the instance with `limactl stop yolobox && yo up`, which drops every flow.

### Telling them apart

Recognise the two by those patterns, and on a macOS host run `scutil --nc list` before debugging anything inside the VM.

`yo status` tells them apart on its `network:` line. It runs `yolobox-guest net-check` as the operator, which takes no arguments and prints three results, the third being a root disk check that comes before any network verdict:

- `egress=`, an HTTPS fetch of github.com.
- `gateway=`, a DNS query for the root name servers sent to `192.168.5.2` with `dig +time=2 +tries=1`. DNS is the one gateway path the [egress table](../nix/egress.md) leaves open on a plain box, and it rides the NAT path above, so a reply proves gvproxy really reaches the host. The probe counts a reply by the `->>HEADER<<-` line `dig` prints, whatever the reply's status, and never by `dig`'s exit code, which bind9 reports as 0 on an ICMP unreachable or a reset connection. `skipped` when `/etc/yolobox/gateway-blocked` exists, see [above](#a-box-that-blocks-the-gateway-on-purpose).

- `rootdisk=`, `full` when `/` has under 64 MiB left for root. A full disk is not a network fault, but it looks like one: dhcpcd cannot write `resolv.conf`, so egress is dead while the gateway answers, exactly the first VPN shape. When it is `full`, `yo` prints that instead of any VPN verdict (see [disk.md](disk.md#a-full-disk-looks-like-a-dns-failure)).

`network_status_line()` maps the first two (when the disk has room). Egress dead with the gateway answering is a VPN that owns the host's default route. Egress ok with the gateway silent is a VPN that blackholes lima's gateway, or a DNS filter on the host that drops the query. Both dead means the guest network itself is down, or both faults at once. A box whose `yolobox-guest` still wants the old `PORT` argument exits 2 when `yo` sends none, and `yo` prints `network: not checked` with the reason. The rest of `yo status` still runs.

### A fake-IP proxy on the host

A fake-IP proxy on the host answers every name with an address in `198.18.0.0/15` and proxies the connection itself. The guest's DNS goes through the host's resolver, so the box gets those addresses too and reaches the internet through the proxy. This works: the [egress table](../nix/egress.md) does not block `198.18.0.0/15`.

## The DNS sinkhole

A DNS filter on the host that sinkholes the global `sts.amazonaws.com` name breaks a guest that resolves through lima, as boxes do by default. Credentials arrive, but `aws sts get-caller-identity` dies with `Could not connect to the endpoint URL: "https://sts.amazonaws.com/"`.

That error is DNS, not AWS. The filter answers the global name with 0.0.0.0. A guest that uses lima's resolver (192.168.5.2) inherits the host's resolver and with it the filter. The AWS CLI targets the global endpoint only when the profile has no region. Regional endpoints such as `sts.eu-west-1.amazonaws.com` resolve fine on both sides.

That is why a profile without a region is left out of the [allowlist](aws.md#allowlist) rather than shipped broken.
