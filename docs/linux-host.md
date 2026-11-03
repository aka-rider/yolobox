# The Linux host

A Linux PC runs the box as a libvirt domain on `qemu:///system`, with its network from passt and its confinement from SELinux. The guest, the accounts, the egress table and the ssh model are the Mac's, unchanged. This file says what is different on the host, why, and how to recognise each way it breaks. The one-time host setup is in [linux/host-setup.md](../linux/host-setup.md), the driver in [yo.md](../yo.md#the-libvirt-driver), and the guest-side table in [nix/egress.md](../nix/egress.md).

## Why qemu:///system and passt

The box has to hold three promises on a Linux host: the guest cannot reach the host or the LAN, no guest port lands on the host unasked, and the guest cannot undo a rule that protects the host. Each design that was not chosen fails one of them.

- **Lima.** Lima on Linux runs QEMU as the operator, with no sVirt category of its own, and QEMU and its network helper carry the operator's uid. A host filter keyed on the uid could not tell the guest's traffic from the operator's own, and a guest that escaped QEMU would hold everything the operator holds.
- **`qemu:///session`.** The same, by construction: the session daemon runs as the operator. It has no sVirt category separation between domains and no way to run a domain under another account.
- **A bridge or libvirt's `default` network.** The guest then has a layer 2 path to the host and the LAN, and libvirt's NAT network puts a host interface in the guest's reach. `host-setup.sh` switches the `default` network off, and the domain template has exactly one interface, of type `user`.
- **`qemu:///system` with passt.** libvirt labels each domain with its own SELinux category pair (`svirt_t:s0:cN,cM`) and starts passt for it, in `passt_t` with the same pair. The domain also carries a static DAC label, so QEMU and passt run as the account `yolobox-vm`. That uid is what lets a host nft table judge exactly the guest's traffic.

passt has no destination filter. It turns every guest connection into a `connect()` on the host, and a guest connection to any of the host's own addresses (LAN, tailnet, IPv6) is delivered over the host's loopback. So the host table is not optional, and the guest's own egress table is the second layer, not the only one.

## The Lima mirror

The domain reproduces Lima's network so that `nix/egress.nix`, its test fixtures and `net-check` need no change:

- the guest is 192.168.5.15/24 on `enp0s1` (the interface is pinned to PCI slot 1 so the name does not move);
- the gateway is 192.168.5.2, which passt maps to the host's loopback, as gvproxy does on a Mac;
- passt serves DNS at the gateway, and only when the host's first resolver is on its loopback (`host-setup.sh` refuses otherwise);
- the one thing the host reaches in the guest is port 22, through a `portForward` on `127.0.0.1`.

`libvirt/domain.xml`, `nix/egress.nix` (`limaGateway`) and `tests/egress_rules.py` all spell the gateway, and `tests/test_consistency.py` keeps them equal. libvirt hands passt the gateway only since 12.2.0, which is why the host setup gates on that version.

## The host table

`linux/yolobox-host.nft` hooks output and judges uid `yolobox-vm` only: loopback is open to DNS and nothing else, and the private, CGNAT, link-local and multicast ranges are rejected, exactly the sets of `nix/egress.nix` (v6 adds `::1`). The reasons for the unit's shape are in [linux/host-setup.md](../linux/host-setup.md#the-host-table):

- no `ExecStop`, so stopping the unit never opens the host;
- `RequiredBy=virtqemud.service` and `Before=`, so no domain can start while the table is missing;
- a static `yolobox-vm` account, because the table names it and nft resolves the name when it loads the file.

Recognise a missing table with `nft list table inet yolobox-host` (needs root) or `systemctl is-active yolobox-host.service`. `yo` can only see the unit, not the table (see `TODO.md`). A working layer shows up from inside a box as a `curl` to the host's LAN address that fails at once while DNS still answers. `tests/box-libvirt-check.sh` proves it by deleting the guest table first, so the host layer is the only thing in the way.

## Host ports

Nothing in the guest is mirrored onto the host. The only ports on the host's `127.0.0.1` are each box's ssh forward and the forwards `yo port GUEST_PORT` opens, and both come from one window: every port above the second field of `/proc/sys/net/ipv4/ip_local_port_range`, which is 61000-65535 with the kernel default.

- The kernel never picks a port in that window as a source port, and no registered or well-known service lives there. Lima's mirror needed a deny-list of OAuth callback ports because it mirrors everything in a range. A window above the ephemeral range needs no list, which could always miss one.
- `yo` reads the range at run time. A host that moved the range's end up to 65535 has an empty window, and `yo` refuses to run, naming `net.ipv4.ip_local_port_range`. With no free port left in it, `yo` says `no free host port in A-B`.
- The window is also the cap. It holds every instance's ssh forward and every open `yo port` forward, so a default host fits about 4,500 of them together.
- Choosing a port and taking it is one step under a lock, so two `yo` processes never pick the same port. A port is free when nothing is bound to it on `127.0.0.1` and no yolobox-tagged domain forwards it.
- The `portForward` address is always `127.0.0.1`. A `portForward` without an address binds every host address, which would put the box's sshd on the LAN. `config_gaps()` treats any other address as a fatal gap.

## The operator's key

A Mac gives the operator a private key file under `~/.lima`, which the constitution lists as a pre-existing exception. A Linux host adds no such file. The operator is a 1Password SSH Key item titled exactly `yolobox-operator`. `yo` requires exactly one agent identity with that comment, writes only its public half (`operator.pub`) and puts it in the cidata. `ssh.config` sets `IdentityAgent` to the host's 1Password socket (`~/.1password/agent.sock`, the Mac's socket is elsewhere), `IdentityFile` to `operator.pub` and `IdentitiesOnly yes`.

Every login to the box, the operator's, the agent account's and root's in the initrd, needs that key from that agent. So a command that must log in asks the agent for its keys first, through `LibvirtDriver.require_login_key()`. A missing socket, a refused connection or an empty key list stops the command with `1Password's SSH agent at <path> is not running (or locked) — start or unlock 1Password`, before any ssh. Without the check a dead agent showed up as `unreachable — is it up?`, and each failed login added to sshd's per-source penalty on the gateway address (see [nix/base.md](../nix/base.md#sshd-penalties)). The check runs in one place per path:

- `vm_up()` runs it before it starts or unlocks anything. That covers `yo up`, both of `yo bootstrap`'s starts and `yo disk-grow`'s. The second start of a bootstrap, after a long `nixos-rebuild`, reuses the passphrase it derived at the first, so it never asks 1Password; without the check a 1Password that quit or locked meanwhile surfaced as a refused key, read as "reading the key from cidata failed".
- `main()` runs it for every other command whose parser sets `logs_in` (`ssh`, `enter`, `cp`, `code`, `zed`, `gc`, `aws-check`, `disk-grow`), before it heals or runs anything.
- `yo status` prints the agent problem as a `login:` line and reports the rest.
- `yo port` rides the operator's ssh master, which needs no agent once it is open. `ensure_master()` runs `true` through the master first, because `ssh -O check` proves only that the master process answers, not that its connection lives. Only when that session fails does it drop the master (`-O exit`), check the key and log in again. The `-O` control commands never log in, and nothing checks them.
- `yo down` needs no login to stop a box, but it must know whether the box waits in its initrd: an initrd ignores a shutdown request, and powering it off can cut the first boot's in-place encryption. With a dead agent, an sshd that sends no `yolobox-stage2` banner and refuses both the operator and `root` could be either, so `boot_stage()` returns `UNKNOWN`, not `NONE`. `yo down` then stops nothing, and `yo up` refuses too. Both print the way out (`vm.unknown_stage_message()`), and `yo status` prints it as its `vm:` line, without running the pinned probes a possible initrd would trip: first fix the login when that is the cause (this host's 1Password, named only on libvirt); then read the console (`virsh console`), because the migrate unit's line `Encrypt or finish encrypting /dev/vda2 in place` started and not finished means the disk is being encrypted and must not lose power; once the console shows the passphrase prompt without that unit running, `virsh destroy` is safe, then `yo up`. None of the three points at another of them, so the guidance does not loop. `NONE` is left for a port where nothing answers and for a marked stage 2 that refused the operator; a stage 2 handles a shutdown request, so there `yo down` asks for one, as before.

`operator.pub` is written `0600` though it is public: when ssh has no agent it reads the `IdentityFile` as a private key, and on any wider mode it warns `UNPROTECTED PRIVATE KEY FILE`, which misnames the failure. At `0600` it says `Load key ... invalid format` instead. An instance directory written by an older `yo` keeps `0644` until the next start rewrites the file.

That key is served by the same 1Password agent the guest's agent account reaches through the forwarded socket. So the agent could try to log in to its own box as the operator. Two rules in `nix/base.nix` stop it. They apply on every box, a Mac's included, because the guest is never told which host runs it (see [nix/base.md](../nix/base.md#no-host-os-in-the-guest)):

- `AllowUsers` admits the operator only from 192.168.5.2, where the host's forwarded port arrives from, and the agent account from anywhere. An ssh to the guest's own address, or to `127.0.0.1`, comes from another source and the operator is refused.
- A firewall rule, `! -i enp0s1 -s 192.168.5.2 -j DROP` on `nixos-fw`, drops any packet that claims that source on an interface other than `enp0s1`, so the agent cannot claim it over loopback.

`tests/box-hardening-check.sh` checks both from inside the guest: the agent's ssh as the operator fails with `Permission denied` at 127.0.0.1 and 192.168.5.15, and the rule is in `nixos-fw`. A box that fails the first shows the operator logging in from the agent's session. The tailnet is not an entry for the operator on any box: the rule admits the gateway address only.

## Config gaps are fatal

On a Mac an instance that differs from `lima/yolobox.yaml` mostly prints a note, because Lima's config is read once and a drift is usually a missing forward; only the `ssh.overVsock` gap refuses there (see [lima/yolobox.md](../lima/yolobox.md#checking-a-running-instance)). A libvirt domain that is not what the template made may hold a path to the host, so `vm_up` refuses to start it. The check is an allowlist, not a list of forbidden devices: the root's children may be only the elements of `libvirt/domain.xml` plus the few libvirt adds when it defines a domain (`uuid`, `currentMemory`, `resource`), and the children of `<devices>` only the template's tags and the few libvirt adds (the emulator path, a ps2 `input` pair, a silent `audio`, the q35 `watchdog`), each with the `type` values the template uses (an `rng` has none, so its `<backend>` is judged instead: only `random` reading `/dev/urandom` or `/dev/random`, because `egd` over a unix socket reaches a host socket and `random` reads any file its text names, so `<backend model='random'>/home/op/secret</backend>` is the gap `device:rng[backend=random:/home/op/secret]`) (`DOMAIN_ELEMENTS_ALLOWED` and `DEVICES_ALLOWED` in `yolobox_cli/libvirt_host.py`). Anything else is a gap, named after what it is: `device:shmem`, `device:hostdev`, `device:serial[type=tcp]`, or `element:qemu:commandline` for a root-level element. A deny-list of six tags missed `shmem`, `redirdev`, `smartcard`, `parallel`, a passthrough `input`, a `serial` or `console` that was not a pty, and a root-level `qemu:commandline`, each of which can open a path between the guest and the host. The other gaps are a missing SELinux or DAC seclabel (or a DAC label that is not the current `yolobox-vm` uid and gid), a missing metadata tag, a `portForward` not bound to 127.0.0.1, and a network that is not exactly one passt interface of type `user`. The message names the gap and the way out. There is no `yo` command to delete an instance yet (see `TODO.md`), so recreate one by hand with `virsh`.

## IPv6

A Mac box has no global IPv6: Lima's NAT gives it only a link-local address. passt copies the host's IPv6 configuration into the guest, so a box on a Linux host with global IPv6 has a global address and a v6 default route. The host table and the guest table reject the same v6 ranges (`fc00::/7`, `fe80::/10`, `ff00::/8`, and `::1` on the host) and the host's own global v6 address is delivered over loopback, which the host table rejects. But a LAN machine with a global address cannot be told from the internet, the same hairpin that [nix/egress.md](../nix/egress.md#residuals) records for a public IPv4 address.

## The tailnet residual

The guest table accepts everything that leaves by `tailscale0` (the tailnet's access policy is the gate, as on a Mac). If the host is on the tailnet and its sshd listens on `0.0.0.0:22`, a box on the same tailnet can connect to the host's tailnet address on port 22. The agent holds the forwarded 1Password agent, so if a key that the vault serves is in the host's `authorized_keys`, a tailnet-joined box could log in to the host.

Close it on the host: bind sshd to the LAN address or loopback with `ListenAddress`, or drop `tailscale0` traffic to port 22, and make sure the tailnet policy does not let a box's node reach the host's node. `tests/box-libvirt-check.sh` prints `RESIDUAL tailnet-host-ssh` when the connection succeeds, and `SKIP tailnet` when the host or the box is not on a tailnet. The residual does not fail the check. Accepted, because boxes are meant to reach the tailnet.

## polkit

The rule `host-setup.sh` writes grants `org.libvirt.unix.manage` on `qemu:///system` to the operator's account. That is root-equivalent for the operator's own host processes: a process of theirs can define a domain with a host device or a file path in it. It is accepted because the account is already in `wheel`, and the alternative is a root password on every `virsh` call. It is not a defence against a compromised operator account; the box defends the host against the guest. A rule limited to the actions `yo` uses would narrow it, if libvirt's polkit actions were fine-grained enough to express that.

## Stopping the domain

`LibvirtDriver.stop` arms one `virsh event NAME lifecycle --loop --timeout 300` before it asks for the shutdown, never re-arms it, and re-reads `virsh domstate` after arming and after every line the watcher prints; it stops when the state is exactly `shut off`. An earlier version re-armed a one-shot watcher after each event, and a "Stopped" event that landed in the gap before the new watcher registered was lost, so the stop waited the full 300 s on a domain that was already off. A timeout is virsh printing `event loop timed out` and exiting 1, and any other exit is a virsh failure; both end with an error that names `virsh destroy`.

A domain parked in its initrd (the disk is locked) has no ACPI handler, so `virsh shutdown` would only run into that timeout. `yo down` therefore asks the initrd what it is doing (`yo.md`, "Starting and unlocking") under the box's unlock lock. A box idle at its passphrase prompt gets `virsh destroy` without a shutdown request (a domain already off is left alone); a hard power-off there loses nothing, since nothing is written. A box that is encrypting its disk in place is refused with no command offered, because `virsh destroy` mid-encryption can destroy the disk. The other refusals (a leftover unlock helper, a failed migration, an unlocked initrd that cannot mount the root, a probe that could not be judged) print `virsh -c qemu:///system destroy DOMAIN` with the condition under which it is safe, and never run it. Recognise the leftover helper by a `yo down` that keeps refusing a locked box that no `yo up` is touching.

An open residual remains: virsh gives no signal that its watcher has registered. A shutdown that completes between the first `domstate` read, right after the watcher is spawned, and the watcher's registration is still missed, and the stop then waits the full timeout. The window is milliseconds, and only a domain that is already `in shutdown` when `yo down` runs (a guest that powers off by itself at that instant) can fall into it. Recognise it by a `yo down` that fails with `did not shut down within 300 s` while `virsh domstate` already says `shut off`.

## The passt socket directory

libvirt creates `/run/libvirt/qemu/passt` owned by `qemu` with mode 0755, and passt, which runs as `yolobox-vm`, binds its socket there. `host-setup.sh` step (j) therefore adds an ACL entry for `yolobox-vm` (`/etc/tmpfiles.d/yolobox-passt.conf`, applied at every boot because `/run` is a tmpfs). The directory has no sticky bit, so a compromised yolobox passt or QEMU, which runs as `yolobox-vm`, could unlink the passt sockets of other, non-yolobox domains in that directory. That disrupts their networking; it does not reach into them. Accepted. Running every host VM as `yolobox-vm` would close it and was rejected, because it changes foreign VMs.

The ACL is also fragile. When libvirt starts another passt domain later it resets the directory, the ACL mask is recomputed to `r-x`, and the `yolobox-vm` entry's effective permission falls to `r-x` with it. `getfacl /run/libvirt/qemu/passt` then shows `user:yolobox-vm:rwx #effective:r-x` and `mask::r-x`, and a yolobox `virsh start` fails with a Permission denied from passt. `yo up` checks the effective permission without privilege, right before `virsh start`, and refuses with the remedy below; no other command looks at it, because only a start needs passt to bind. `TODO.md` has the durable fix that is still missing.

## The disk unlock

The LUKS passphrase is sent to the initrd's sshd through the box's forwarded port. Before each send, `verify_initrd_listener` runs `ss -ltnHe` on the port and requires that every listener is owned by `yolobox-vm`, because another local account could otherwise bind that port and receive the answer to the handshake. The port is above the ephemeral range, so only a deliberate squat can hold it. A refusal reads `... is held by uid N, not by the account yolobox-vm`. The check is not atomic with the connect, and a port with no listener passes it: `TODO.md` has the remaining gap.

## Sizing a box

A box gets its memory and disk when `yo` creates its domain: `yo up --memory 24 --disk 250` (or `yo bootstrap` with the same flags) creates one with 24 GiB of memory and a 250 GiB disk. Without the flags a box gets 16 GiB and 100 GiB (`domain_size()` in `yolobox_cli/libvirt.py`). vCPUs stay at 8. The floors are 8 GiB of memory and 40 GiB of disk, for the reasons in [yo.md](../yo.md#several-machines): the box builds its own system, and it holds a 16 GiB swapfile plus the nix store of its first build. A value under a floor, or anything that is not plain ASCII digits for a whole number of GiB above 0, exits 2 before anything runs. An explicit `--memory` that does not fit beside what already runs is refused too, before the passphrase is asked for: the asked memory plus the max memory of every other active domain on `qemu:///system` must be at most the host's `MemTotal` (`/proc/meminfo`) less a 4 GiB reserve for the host (`HOST_MEMORY_RESERVE_GIB`). The default is not held to it, because a 16 GiB host reports a little less than 16 GiB.

The rule counts each domain at its full max memory because the template gives a guest no balloon (`memballoon model='none'`): qemu's resident memory grows towards all of it as the guest touches pages, and nix builds touch them all. A guest's swapfile does not help the host. The earlier rule only compared `--memory` with `MemTotal`, so a box asked for most of `MemTotal` beside another running domain passed, and the host's OOM killer ended a qemu once the guests touched their memory. Active, not only running, domains count, because a paused domain keeps its memory. Shut-off ones do not, so starting one later can still overcommit the host; that is the operator's call. A domain undefined between `yo`'s listing and its `dominfo` is not counted; any other `dominfo` failure is an error. The reserve is a guess at what a desktop host needs, not a measurement, and the rule ignores host swap. Every `virsh` runs with `LC_ALL=C`, because virsh translates what it prints (`Max memory`, the `domstate` words) and `yo` parses it.

For a box whose domain exists, both flags are refused before the passphrase is asked for, and the refusal names the way out for what was asked:

- **Memory alone** lives in the domain, not on the disk. `yo -m NAME down && virsh -c qemu:///system undefine --nvram yolobox-NAME` removes the domain and keeps the disk volume, and the next `yo up --memory N` defines a new domain with that memory and adopts the volume as it is.
- **The disk** cannot change in place. The first boot encrypts the root, and growing an encrypted libvirt disk is not implemented (`yo disk-grow` raises here, `TODO.md`). A new size means deleting the box, and everything on it is lost: the refusal prints the whole delete (`yo down`, `undefine --nvram`, both `vol-delete`s and the instance directory), after which `yo up --disk N` creates the box again.

After a `virsh undefine` the volume is still there, and the next start adopts it at the size it has. An explicit `--disk` that differs from that size would be ignored silently, so `creation_refusal` refuses it and names the two ways out: drop `--disk` to keep the disk, or `virsh -c qemu:///system vol-delete --pool default yolobox-NAME.qcow2`, which loses what is on it, to build a new one. `--memory` alone passes there, since the new domain carries it.

## A domain that dies under yo

A domain can go away while `yo` waits for it: the host's OOM killer ends its qemu, qemu crashes, or someone runs `virsh destroy` or `virsh undefine`. `unlock_disk` and `wait_for_operator` read `virsh domstate` between probes, and any state but running ends the wait at once with an error like `NAME is shut off, not Running, while yo waited for its sshd to accept the operator`. What follows depends on the state (`LibvirtDriver.vm_lost_hint`):

- `shut off`: `virsh -c qemu:///system domstate --reason yolobox-NAME` says why, the guest was possibly killed by the host's OOM killer, and `journalctl -k -g oom` is where to check; a kill shows there as an `Out of memory: Killed process` line naming qemu.
- `paused` or `pmsuspended`: the same `domstate --reason`, and the command that brings it back, `virsh -c qemu:///system resume yolobox-NAME` or `virsh -c qemu:///system dompmwakeup yolobox-NAME` (`state_hint`).
- any other state: check why with `domstate --reason`.
- the domain is gone (`not listed`): `yo up` defines it again and adopts the disk.

The way back after an OOM kill is less memory or more free memory on the host. The domain keeps the memory it was defined with, so `yo -m NAME down && virsh -c qemu:///system undefine --nvram yolobox-NAME`, which keeps the disk, then `yo -m NAME up --memory N`. A `yo bootstrap` that was rebooting into its new generation finishes when it is run again: the built generation is already the boot default.

## Recognising failures

- `yo` refuses with a message naming `host-setup.sh`: a precondition is missing. The message says which: `virsh`, the connection to `qemu:///system`, the daemon's version, `yolobox-host.service`, the `yolobox-vm` account or the port window.
- `yo up --memory` or `--disk` says the machine `already exists`: the domain is there, and the message names the way out for memory or for the disk (see [Sizing a box](#sizing-a-box)). `... already holds a finished N GiB disk`: the domain is gone but its volume is not, and `--disk` asks for another size. `--memory N does not fit on this host`: ask for at most what it names, or stop one of the domains it lists (see [Sizing a box](#sizing-a-box)).
- `yo up` or `yo bootstrap` ends with `... is shut off, not Running, while yo waited for ...`: the domain went away under `yo`, possibly by the host's OOM killer (see [A domain that dies under yo](#a-domain-that-dies-under-yo)).
- `yo up` raises a gap message before asking for the passphrase: the live domain differs from the template. Read the gap, then recreate the domain.
- `virsh start` fails with `Failed to bind UNIX domain socket: Permission denied` from passt: `yolobox-vm` cannot write to `/run/libvirt/qemu/passt`. Run `getfacl` on it: a missing `user:yolobox-vm:rwx` entry means the tmpfiles rule never applied, and an entry that reads `#effective:r-x` with `mask::r-x` means the mask was recomputed after libvirt started another passt domain (see [The passt socket directory](#the-passt-socket-directory)). Either way the remedy is `sudo setfacl -m u:yolobox-vm:rwx /run/libvirt/qemu/passt`, which also recomputes the mask. `systemd-tmpfiles --create` is not a remedy: it appends the entry and leaves `mask::r-x`. The rule `host-setup.sh` installs names the mask too (`m::rwx`), so a fresh boot starts right; running `host-setup.sh` again is not needed. Starting a non-yolobox passt domain can break it again. `yo up` prints this remedy, instead of starting, when the effective permission is short.
- `virsh start` fails and the audit log shows an SELinux denial for passt: `passt-selinux` is missing. A start that fails on the gateway option means the running libvirt daemon is older than 12.2.0 (`virsh version --daemon`).
- A guest that reaches the host's LAN address, or the host's own services: the host table is missing or the unit is inactive, and the guest table is also gone or in a fallback tier (`yo status` prints which: `recovery table`, `last-resort table`, or `no table (fail-open)`, see [nix/egress.md](../nix/egress.md#the-unit-and-recovery-mode)).
- `ps -eZ` shows `qemu` or `passt` as another uid, or without the domain's category pair: the DAC or SELinux label did not apply. `tests/box-libvirt-check.sh` (b) asserts it.
- `yo` refuses to run, saying the kernel's ephemeral range ends at 65535: lower the range's end with `sysctl net.ipv4.ip_local_port_range`.
- Every `virsh` call asks for a password: the polkit rule is missing or names a different account.
- The guest cannot resolve names: the host's first nameserver is not a loopback address, so passt does not serve DNS at the gateway.
