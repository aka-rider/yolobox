# The Lima instance

`lima/yolobox.yaml` seeds the VM. Lima reads it once, and the rest of this file explains what that means for a running box, which settings lima leaves to the seed, which guest ports are mirrored onto the host and which are not, and one lima bug that kills every forwarded port.

## Read once

Lima copies `lima/yolobox.yaml` into `~/.lima/yolobox/lima.yaml` when it creates the instance, and reads only that copy afterwards. Editing the repo file changes nothing on an existing instance. A new port-forward rule in the repo file is inert until someone restates it on the instance.

Restating `portForwards` means stopping the VM and restating the whole array, because lima's own yq cannot read the repo file. The array is `LIMA_PORT_FORWARDS`, and it also carries the two reverse socket rules. Do not hand-write it. A hand-written copy went stale once, and run as written it dropped the socket rules. Run the `limactl edit` command that `yo up` prints, which `lima_config_gap_message()` builds from the constant.

## Checking a running instance

Before it starts the VM, `vm_up()` runs `limactl list yolobox --json` and reads the running instance's own `config.portForwards`. It never searches the text of `lima/yolobox.yaml`. For each of `OP_GUEST_SOCK` and `AWS_BROKER_GUEST_SOCK`, it looks for an entry whose `guestSocket`, `hostSocket` and `reverse: true` all match.

A missing rule prints a stderr note from `lima_config_gap_message()`, and `vm_up()` carries on. It can carry on because `heal_reverse_forward()` adds the forward live, over lima's own ssh master, with a bare `-O forward -R <guest>:<host>`. That needs no matching entry in `portForwards` (see [Healing reverse forwards](../docs/ssh.md#healing-reverse-forwards)). The restated array is what makes the forward permanent: it survives the next hostagent restart with no `yo` run to heal it again.

With no instance at all, `limactl list` fails and the check prints nothing, because a brand-new box has no gap to report. The check exists for an instance created before a rule existed. Such a box has only the dhcp and 1Password rules, since the AWS broker's rule came later, and its UDP rule ignores port 68 alone (see [Guest UDP is never mirrored](#guest-udp-is-never-mirrored)). The check compares the socket rules only.

The same check reports four more gaps as stderr notes: a mounts gap, a `forwardAgent` gap, a UDP gap (an instance with no rule that ignores all guest UDP on every address, see below) and a TCP gap (see [Which guest ports reach the host](#which-guest-ports-reach-the-host)). All four are settings lima reads once. The UDP and TCP gaps are restated with the same full-array command as the socket rules. `lima_config_gap_message()` gives each gap its own problem line and prints that command at most once.

## Guest UDP is never mirrored

Lima mirrors every port a guest process listens on onto the host's localhost, UDP included. Nothing here needs a UDP one: the secrets travel as reversed unix sockets. So `lima/yolobox.yaml` ends its `portForwards` with one rule, which ignores every guest UDP port:

```yaml
- proto: udp
  guestIP: 0.0.0.0
  guestIPMustBeZero: false
  ignore: true
```

Both `guestIP` lines are needed, and the first version of this rule (v1.5.0) left them out. It said only `proto: udp` and `ignore: true`, on the belief that lima's default `guestIP` of 127.0.0.1 also covers listeners bound to `0.0.0.0` and `::`. It does not. The live host agent kept mirroring UDP 68 and 41641 (`lsof -nP -iUDP -a -c limactl` showed `*:68` and `127.0.0.1:41641`). The rule is matched in `forwardingAddresses()` in lima's `pkg/portfwd/forward.go` (v2.2.1; `pkg/hostagent/port.go` holds the same loop for TCP). `guest` is the listener, `rule` is a `portForwards` entry, and the rule's `guestPort` range defaults to every port:

```go
switch {
case guestIP.IsUnspecified():                                    // listener on 0.0.0.0 or ::
case guestIP.Equal(rule.GuestIP):                                // listener on exactly the rule's address
case guestIP.Equal(net.IPv6loopback) && rule.GuestIP.Equal(IPv4loopback1):
case rule.GuestIP.IsUnspecified() && !*rule.GuestIPMustBeZero:   // a wildcard rule matches any address
default:
    continue
}
if rule.Ignore {
    if guestIP.IsUnspecified() && !rule.GuestIP.IsUnspecified() {
        continue // an ignore rule does not cover a wildcard listener unless it is a wildcard rule
    }
    break
}
```

So lima treats the two kinds of listener differently:

- A wildcard listener (`0.0.0.0` or `::`, which is what tailscaled's 41641 and the DHCP client's 68 are) passes the first `case` for any rule, but an ignore rule then skips it unless the rule's own `guestIP` is unspecified. With the default 127.0.0.1 the rule is skipped and the later default rule forwards the port. The loopback-only rule hid exactly the listeners that matter here.
- A listener on a specific address (127.0.0.1, `::1`, the guest's LAN address) matches a wildcard rule only through the fourth `case`, which needs `guestIPMustBeZero` false. Lima fills it in as true whenever `guestIP` is 0.0.0.0, which makes 0.0.0.0 an exact match, so the rule would then cover wildcard listeners only. That is how the old port-68 rule (`guestIP: 0.0.0.0`) worked, and why it was enough for DHCP and not for 127.0.0.1:41641.

`guestIP: 0.0.0.0` with `guestIPMustBeZero: false` is the one spelling that ignores both kinds. (`guestIP: "::"` does the same, since lima only asks whether the address is unspecified. Lima's own code blocks its SSH port the same way.) The default range is every port, so the rule covers tailscaled's UDP 41641, its ephemeral UDP sockets and anything the agent runs later. It replaces the earlier rule that ignored UDP 68 alone ([nixos-lima#50](https://github.com/nixos-lima/nixos-lima/issues/50): the mirror intercepted the Mac's DHCP packets), which was the same fault for one port. `ignores_all_guest_udp()` in `yo` applies the same test to a running instance, so a rule that lacks either line is reported as a gap.

The failure that earned the rule is in `~/.lima/yolobox/ha.stderr.log`, the host agent's log. It starts fresh with each host agent start, and within the first second it holds a few lines like

```
{"error":"listen udp 127.0.0.1:41641: bind: address already in use","level":"warning","msg":"failed to listen udp: 127.0.0.1:41641",...}
```

The guest's tailscaled listens on UDP 41641, lima tries to mirror that onto the host's loopback, and the host's own Tailscale already holds the port. Seen on a live box: four such lines in the first second of a host agent start, and none after, so the line is a start-up note and not a loop. The mirror was never in the way of tailscaled's own packets, which leave through the NAT and not through the mirror. The cost is those lines and, on a Mac without Tailscale, a UDP port on the Mac's loopback that would forward datagrams into the guest's tailscaled. That second part is why the rule ignores all guest UDP and does not just name 41641.

### Applying it to an existing instance

The instance still has its old rule set, so the mirror keeps running there until it is restated. `yo up` and `yo status` now notice that: an instance with no rule that ignores all UDP on every address, v1.5.0's loopback-only one included, gets a note that names the problem and prints the command. It is the same command as for a missing socket rule, because lima's yq cannot merge into the array and the whole array must be written back:

```sh
limactl stop yolobox && limactl edit yolobox --set '.portForwards = <the array yo prints>' && yo up
```

Take the array from `yo up`'s note, never from this file (see [Read once](#read-once)). Editing `~/.lima/yolobox/lima.yaml` by hand does the same job, but `limactl edit` also validates the result. Stop the VM first, as for every `portForwards` restate: lima reads the file at the next host agent start. The old rule for port 68 leaves with the restate, because the new rule covers it.

Verify with `limactl list yolobox --json`: `config.portForwards` must hold an entry with `"proto":"udp"`, `"ignore":true`, `"guestPortRange":[1,65535]`, `"guestIP":"0.0.0.0"` and `"guestIPMustBeZero":false`. Then `lsof -nP -iUDP -a -c limactl` must show no socket bound to `*:68` or `127.0.0.1:41641` (the outbound `10.x->peer` flows are gvproxy's NAT and stay), and the next host agent log must have no `Forwarding UDP` and no `failed to listen udp`.

## Which guest ports reach the host

Lima mirrors a guest TCP listener onto the host's `127.0.0.1` only when its port is in 1024 to 49151, and never onto any other host address. Every other port and every protocol other than TCP is ignored. The range is the unprivileged ports below the macOS ephemeral range (`sysctl net.inet.ip.portrange.first` prints 49152), so a mirror can neither take a privileged port nor collide with an outgoing connection's source port. A Linux host would need 1024 to 32767, because the Linux ephemeral range starts at 32768. That is recorded here only, because yolobox runs on macOS.

Lima ends a `portForwards` list with its own default rule, protocol `any`, guest 127.0.0.1, ports 1 to 65535 onto the host's 127.0.0.1. On macOS that default also forwards ports below 1024. (A rule's `proto` defaults to `any`, not `tcp`.) So `lima/yolobox.yaml` has to end with catch-all ignore rules, and the order of the list is part of the setting, because lima takes the first matching rule (`pkg/portfwd/forward.go`, `pkg/limayaml/validate.go` in v2.2.1):

1. **The deny-list**, each rule `proto: tcp`, `ignore: true`. These are fixed OAuth callback ports, which a host-side login tool expects to bind itself: 1455 to 1457 (Codex CLI), 8020 (Databricks), 8085 (gcloud), 9005 (Firebase) and 18000 (kubelogin). A guest listener on one of them would answer a browser redirect that belongs to the host. AWS SSO, the Azure CLI and the GitHub CLI pick random ports and need no entry. Port 8000, which kubelogin also uses, stays mirrored on purpose: it is the Django and `http.server` default. The names and ranges live in `OAUTH_CALLBACK_PORT_RANGES` in `yo`.
2. **The range rule**, `guestPortRange: [1024, 49151]` with `proto: tcp`. `guestIP`, `hostIP` and the host ports stay at lima's defaults: loopback on both sides, the same port number. It is `MIRRORED_TCP_PORTS` in `yo`.
3. **The reverse socket rules.** They match sockets and not ports, so they could sit anywhere.
4. **The UDP catch-all**, see [Guest UDP is never mirrored](#guest-udp-is-never-mirrored).
5. **The TCP catch-all**, `proto: tcp`, `ignore: true`, with `guestIP: 0.0.0.0` and `guestIPMustBeZero: false`. It needs the same two lines as the UDP rule, for the same reason. Without them it would match loopback listeners only and wildcard ones would slip through to lima's default rule. The deny-list rules carry both lines too.

The TCP and UDP catch-alls stay two rules and never become one `proto: any` rule, because `ignores_all_guest_udp()` and the UDP test in `tests/test_consistency.py` pin the exact UDP rule.

Services still reach the host in these ways:

- **Through lima's forwarder.** A service in range that listens on a wildcard address (`0.0.0.0` or `::`) or on loopback is at `http://localhost:<port>` on the host with no setup. Lima publishes it on the host's `127.0.0.1` alone. No rule here has `hostIP: 0.0.0.0`, and `TestHostLoopbackMirroring` keeps it so.
- **Over the tailnet.** A service bound to the box's tailnet address alone is not on lima's list, so lima does not forward it. It is reachable over the tailnet only, at that address, by whatever the tailnet's access policy admits (`tailscale0` is trusted, see [nix/tailscale.md](../nix/tailscale.md#firewall)).

Recognise the first case in `~/.lima/yolobox/ha.stderr.log`. A service listening on `::` logs `Forwarding TCP from [::]:5137`. The same service rebound to the tailnet address logs `Not forwarding TCP <tailnet-ip>:5137`, and `localhost:5137` on the host stops answering. Nothing is broken: the service moved out of the set lima forwards. Rebind it to `0.0.0.0` to get `localhost:<port>` back. A service in the deny-list or outside 1024 to 49151 is never forwarded, whatever it listens on.

### Applying it to an existing instance

An instance created before the range keeps its old list, which mirrors every port. `lima_config_gaps()` reports the TCP gap when the instance's TCP rules differ from the ones in `LIMA_PORT_FORWARDS`. It cannot compare the rules as written, because `limactl list --json` returns them with lima's defaults filled in (`"guestIP": "127.0.0.1"`, `"proto": "any"`, `"hostPortRange"` and so on). So `normalize_port_forward()` fills the same defaults into both sides, as `FillPortForwardDefaults` in lima v2.2.1 does, and compares them in order. That one comparison catches a missing deny-list rule, range rule or catch-all, a wrong order, and a leftover rule that is in range but extra, such as an old rule that published a port on every host address. The remedy is the full-array restate that `yo up` prints (see [Read once](#read-once)).

## Pinned settings

`lima/yolobox.yaml` pins four settings that lima leaves at its defaults. Each default is wrong for a box that holds an agent.

- `vmType: vz` names Apple's Virtualization.framework. The rosetta block in the same file applies only to that driver, so the driver is pinned rather than left to lima's default.
- `propagateProxyEnv: false` keeps the host's `HTTP(S)_PROXY` out of the guest. A proxy address the VM cannot reach would break every fetch the box makes.
- `ssh.loadDotSSHPubKeys: false` stops lima copying every `~/.ssh/*.pub` from the host into the guest's authorized keys. The only key that can log in is then the one lima generates for the instance.
- `ssh.overVsock: false` makes lima forward port 60022 to the guest's TCP port 22 and never to vsock. Lima chooses once per VM start, at the moment the guest first answers on port 22, and the initrd sshd that unlocks the disk speaks only TCP. Without the pin the choice would depend on which sshd answers first. The pin applies at the next hostagent start only: under vz a guest reboot restarts the VM in place without a new hostagent, so a hostagent that had chosen vsock keeps proxying to vsock after the reboot and the initrd sshd stays unreachable until the hostagent is restarted. The reasoning is in [docs/disk-encryption.md](../docs/disk-encryption.md#how-yo-reaches-the-initrd).

None of them reaches an existing instance. Restate the three that an older box may lack with `limactl stop yolobox`, then `limactl edit yolobox --set '.propagateProxyEnv = false | .ssh.loadDotSSHPubKeys = false | .ssh.overVsock = false'`. `yo up` prints the exact command for any gap it finds. The `vz` pin changes nothing on an instance that already runs vz, and every box of this repo does.

## An empty page means lima's forwarder died

From the host, `curl -v http://127.0.0.1:<port>/` gets, for a service the guest listens on, `Connection reset by peer` with zero bytes. Inside the VM, the same request returns 200. That asymmetry is the diagnosis. The fault sits in lima's forwarder, not in the service. `~/.lima/yolobox/ha.stderr.log` repeats `tcpproxy: ... grpc: the client connection is closing`.

Lima v2.1.2 through v2.2.0 carry the guest-agent streams and the TCP tunnel on one grpc connection. When the guest agent restarts, the host agent dials a new connection, but an existing listener keeps its old dialer. So every forwarded port accepts and then resets, and it stays that way.

This is a regression from lima PR [#4889](https://github.com/lima-vm/lima/pull/4889), first shipped in v2.1.2. That PR closes the stale grpc connection on reconnect. Before it, the stale connection leaked but still carried traffic, so a box on lima older than v2.1.2 never showed the bug.

The host agent replaces the connection only when the guest agent has been down for 10 s after the event stream ended. A quick `systemctl restart lima-guestagent` stays inside that window and is harmless. A `switch` that re-runs `lima-init` first is not.

Upstream reported it as [lima-vm/lima#5557](https://github.com/lima-vm/lima/issues/5557). [lima-vm/lima#5558](https://github.com/lima-vm/lima/pull/5558) fixed it by dialing through the current guest agent client. Both were closed and merged on 2026-10-03, and the fix shipped in lima v2.2.1 the same day. Only v2.1.2 through v2.2.0 are affected, and upgrading `limactl` on the Mac is the real cure.

### Why it bites here

`services.lima.enable` makes `lima-init` and `lima-guestagent` ordinary units whose text embeds store paths. A nixpkgs bump rehashes them, so `switch` restarts both. Twenty switches on one pin never showed the bug. The first bump did.

The box pins that shut with `restartIfChanged = false` on both units. nixos-lima draft PR [#125](https://github.com/nixos-lima/nixos-lima/pull/125) proposes the same pin for every nixos-lima box. The pin stays even though the lima fix has shipped, because it protects a Mac still on an affected lima. It goes once #125 is merged and the flake's `nixos-lima` input is bumped.

`yo` could not have caught this. Every check ran on the healthy side of the hop. A TCP-connect probe succeeds too, because the handshake completes before the reset.

### Recovery

Run `limactl stop yolobox && yo up`. Nothing lighter restarts the host agent. On an affected lima, upgrade `limactl` first, so the box does not come back on the broken version.
