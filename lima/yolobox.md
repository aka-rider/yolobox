# The Lima instance

`lima/yolobox.yaml` seeds the VM. Lima reads it once, and the rest of this file explains what that means for a running box, which settings lima leaves to the seed, and one lima bug that kills every forwarded port.

## Read once

Lima copies `lima/yolobox.yaml` into `~/.lima/yolobox/lima.yaml` when it creates the instance, and reads only that copy afterwards. Editing the repo file changes nothing on an existing instance. A new port-forward rule in the repo file is inert until someone restates it on the instance.

Restating `portForwards` means stopping the VM and restating the whole array, because lima's own yq cannot read the repo file. The array is `LIMA_PORT_FORWARDS`, and it also carries the two reverse socket rules. Do not hand-write it. A hand-written copy went stale once, and run as written it dropped the socket rules. Run the `limactl edit` command that `yo up` prints, which `lima_config_gap_message()` builds from the constant.

## Checking a running instance

Before it starts the VM, `vm_up()` runs `limactl list yolobox --json` and reads the running instance's own `config.portForwards`. It never searches the text of `lima/yolobox.yaml`. For each of `OP_GUEST_SOCK` and `AWS_BROKER_GUEST_SOCK`, it looks for an entry whose `guestSocket`, `hostSocket` and `reverse: true` all match.

A missing rule prints a stderr note from `lima_config_gap_message()`, and `vm_up()` carries on. It can carry on because `heal_reverse_forward()` adds the forward live, over lima's own ssh master, with a bare `-O forward -R <guest>:<host>`. That needs no matching entry in `portForwards` (see [Healing reverse forwards](../docs/ssh.md#healing-reverse-forwards)). The restated array is what makes the forward permanent: it survives the next hostagent restart with no `yo` run to heal it again.

With no instance at all, `limactl list` fails and the check prints nothing, because a brand-new box has no gap to report. The check exists for an instance created before a rule existed. Such a box has only the t3, dhcp and 1Password rules, since the AWS broker's rule came later. The check compares the socket rules only. The t3 rule now says `proto: tcp` explicitly. An older instance without that word is not a gap, because lima's default protocol is tcp.

The same check reports two more gaps as stderr notes: a mounts gap and a `forwardAgent` gap. Both are settings lima reads once.

## Pinned settings

`lima/yolobox.yaml` pins three settings that lima leaves at its defaults. Each default is wrong for a box that holds an agent.

- `vmType: vz` names Apple's Virtualization.framework. The rosetta block in the same file applies only to that driver, so the driver is pinned rather than left to lima's default.
- `propagateProxyEnv: false` keeps the Mac's `HTTP(S)_PROXY` out of the guest. A proxy address the VM cannot reach would break every fetch the box makes.
- `ssh.loadDotSSHPubKeys: false` stops lima copying every `~/.ssh/*.pub` from the Mac into the guest's authorized keys. The only key that can log in is then the one lima generates for the instance.

None of them reaches an existing instance. Restate the two that an older box may lack with `limactl stop yolobox`, then `limactl edit yolobox --set '.propagateProxyEnv = false | .ssh.loadDotSSHPubKeys = false'`. The `vz` pin changes nothing on an instance that already runs vz, and every box of this repo does.

## An empty page means lima's forwarder died

From the Mac, `curl -v http://127.0.0.1:3773/` gets `Connection reset by peer` with zero bytes. Inside the VM, the same request returns 200. That asymmetry is the diagnosis. The fault sits in lima's forwarder, not in t3. `~/.lima/yolobox/ha.stderr.log` repeats `tcpproxy: ... grpc: the client connection is closing`.

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
