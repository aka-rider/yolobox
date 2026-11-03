# Hardening

The code is `nix/hardening.nix`. It shrinks what an agent session can reach in the guest's kernel and in its setuid binaries, and it keeps everything the box needs to work. The idea comes from [a lowendspirit article](https://lowendspirit.com/discussion/11543/) that trims a NixOS VPS by blacklisting modules, dropping services and adding kernel parameters. That article aims at RAM. This module repeats the exercise aimed at attack surface, and the table below ([The journal](#the-journal)) records every feature we looked at, the verdict and the reason. The comments in the `.nix` file give the short reason for each line. This file holds the full list, so the next person does not redo the inventory.

The threat is the agent. It runs as uid 1000 with no `sudo` (see [docs/accounts.md](../docs/accounts.md#two-accounts)), so the way to root is a kernel bug or a setuid binary. The kernel loads a protocol module on demand when any process opens a socket of that family, which means the agent can pull `bluetooth`, `sctp` or `tipc` into the running kernel with one `socket()` call, and a bug in any of them becomes reachable. Many network-protocol kernel CVEs arrive that way. Blocking the modules the box never uses closes that route for every name on the list. It does not close the class. The modules the box needs stay loadable, and with unprivileged user namespaces on, the agent can still reach some of them as root of its own namespace (see [Known residual](#known-residual-the-surface-behind-user-namespaces)). The setuid wrappers are the second path, and the box keeps only five wrappers: `sudo`, `sudoedit`, `unix_chkpwd`, `newuidmap` and `newgidmap`. The last two carry file capabilities, not the setuid bit.

## How to recognise a breakage

Every item here is a deliberate removal, so a breakage has a known shape. A fix for one box lives in that box's own `/etc/yolobox/local.nix`, because the repo holds generic features (see [docs/distribution.md](../docs/distribution.md#flake-ref-not-a-checkout)). A blocked module is the exception, as its row says.

| Symptom | Which item | Fix |
|---|---|---|
| `socket()` fails with `EAFNOSUPPORT` (address family not supported) or `modprobe` prints nothing and exits 1 for a protocol module | A name in `blockedModules`: the `install <module> .../false` line in `modprobe` config, plus the blacklist | Unblocking a module is a change to this repo, not to `local.nix`. Remove the name from `blockedModules` in `nix/hardening.nix` and add a line to the journal below saying what needs it (a CONSTITUTION rule requires the line). A `lib.mkForce` of `boot.blacklistedKernelModules` or `boot.extraModprobeConfig` in `local.nix` is no fix, because it would also drop nixpkgs' own firewire blacklist and every other modprobe line. |
| A program fails with `EPERM` on `io_uring_setup`, or a database or runtime that uses io_uring runs slower | `kernel.io_uring_disabled = 2` | `boot.kernel.sysctl."kernel.io_uring_disabled" = lib.mkForce 0;` in `local.nix` |
| `nixos-rebuild` stops with "security.wrappers differs from the allowlist" and names an unexpected wrapper | The assertion in `nix/hardening.nix` | If the box needs the wrapper, add its name to `yolobox.hardening.extraWrappers` in `local.nix`. A box that runs a VPN client with a setuid split-tunnel helper needs that helper's wrapper name, for example `[ "<helper-name>" ]`. |
| `su`, `sg`, `newgrp`, `chsh`, `passwd`, `mount` or `umount` fails for the agent, or no longer runs with privilege | The disabled wrappers | Expected. The setuid wrapper is gone. A plain, non-setuid program of the same name can still sit on `PATH` (`su` does, in `/run/current-system/sw/bin`), but it runs with the caller's own rights. The operator who needs `mount` uses `sudo mount`. |
| A dev server or test fixture fails to bind port 80, 443 or any port below 1024 with `Permission denied` or `EACCES` | `net.ipv4.ip_unprivileged_port_start = 1024` | Listen on 1024 or above. Do not lower the floor in `local.nix`: see the port-floor row under [Sysctls](#sysctls) for what that reopens. |
| `dmesg` or `/proc/kallsyms` shows nothing useful, `perf` or `bpftrace` refuses to run as the agent | `kptr_restrict = 2`, `unprivileged_bpf_disabled = 1` | Run the tool with `sudo` as the operator. |

A rebuild that fails the assertion fails before anything is activated, so the running box is never left half-hardened.

## The journal

Verdicts. **blocked** means the kernel will not load it. **removed** means the box no longer ships or sets it. **kept** means we looked and the box needs it. **rejected** means we considered a change and decided against it. **not adopted** means the article does it and we do not.

### Kernel modules

`blockedModules` is the one list. `boot.blacklistedKernelModules` stops the alias autoload, and `boot.extraModprobeConfig` adds `install <module> <coreutils>/bin/false` so an explicit `modprobe` fails too. NixOS has no `/bin/false`, hence the store path.

| Feature | Verdict | Why |
|---|---|---|
| `cfg80211`, `mac80211` | blocked | vz has no wireless hardware. The modules are only code the agent could load. |
| `bluetooth`, `btusb` | blocked | vz has no bluetooth. The bluetooth socket family has a long CVE history and autoloads from `socket(AF_BLUETOOTH)`. |
| `sctp`, `rds`, `tipc` | blocked | Rare transports that autoload from a socket call. Nothing on the box speaks them. |
| `x25`, `psnap`, `llc2`, `atm` | blocked | Legacy link and network layers. Unused. |
| `can`, `ieee802154`, `phonet`, `nfc` | blocked | Fieldbus, radio and phone-modem families. A VM has none of the hardware. |
| `af_key` | blocked | The PF_KEY IPsec socket. Nothing here uses kernel IPsec; tailscale uses WireGuard in userspace. |
| `rxrpc`, `kcm`, `smc`, `qrtr` | blocked | Socket families (`AF_RXRPC`, `AF_KCM`, `AF_SMC`, `AF_QIPCRTR`) that autoload from `socket()`. They serve AFS, TCP message framing, IBM shared-memory links and Qualcomm modems. Nothing on the box uses them. |
| `pppoe`, `l2tp_ppp` | blocked | The `AF_PPPOX` protocols for PPP over Ethernet and over L2TP. The box makes no PPP link. |
| `af_alg` | kept | The `AF_ALG` socket gives user space the kernel crypto API. It is a known source of CVEs, but `cryptsetup` and `libcryptsetup` can reach the crypto API through it, and the LUKS unlock must not risk a boot failure for one more blocked name. |
| `ax25`, `netrom`, `rose`, `appletalk`, `p8022`, `caif`, `n_hdlc` | not listed | Kernel 7.2 and 7.3 no longer ship them, so there is nothing to block. A name that does not exist only makes a dead `install` line. |
| `dccp` | not listed | Removed from the kernel in 6.16. |
| `firewire_core`, `firewire_ohci`, `firewire_sbp2` | kept as nixpkgs has them | nixpkgs already blacklists them. Repeating them would be a second spelling of one fact. |
| `vsock`, `vmw_vsock_virtio_transport` | kept | The lima guest agent talks to the host over vsock. Blocking it kills port forwards and `yo up`'s health wait. |
| `virtiofs` | kept | The rosetta share (`mountTag = "vz-rosetta"` in `nix/base.nix`) is a virtiofs mount. The box has no host mounts otherwise. |
| `binfmt_misc` | kept | Rosetta registers x86_64 emulation through it. |
| `overlay`, `fuse`, `tun`, `bridge`, `veth`, `nf_*`, `xt_*` | kept | Rootless podman (overlay storage, bridge and veth networks), tailscale (`tun`) and the egress table (`nf_*`, `xt_*`) need them. |
| `dm_crypt` | kept | The root disk is LUKS. See [docs/disk-encryption.md](../docs/disk-encryption.md). |
| `security.lockKernelModules` | rejected | It freezes module loading after boot. Podman and tailscale load modules on demand, so they would break. |
| A custom kernel with unused code compiled out | rejected | It loses the nixos binary cache, so every kernel bump becomes a local build. The blocklist gets most of the benefit. |
| Trimming the initrd's modules | rejected | The article does it to save RAM. Here a wrong trim breaks the LUKS unlock, and the box has no console to fix it from. |
| Blocking filesystem modules | rejected | The agent can trigger a load. With a user namespace it calls `mount(2)` as namespace root, and the kernel runs `request_module("fs-<type>")` before it checks whether that type may be mounted without real root. For example `unshare -Urm mount -t romfs none /mnt` as the agent loads `romfs` and then fails with "permission denied". Only filesystems flagged `FS_USERNS_MOUNT` (tmpfs, overlay, fuse and a few more) mount in a user namespace, so for every other type the load runs the module's init code and nothing more. That is a small surface, and the module list has about 45 filesystem names to keep in step. The verdict stays, and the honest reason is that the benefit is small, not that the agent cannot get there. |
| Blocking USB, sound and GPU modules | rejected | vz has none of the hardware, so no probe path loads a driver, and the agent has no way to ask for one. Nothing to gain. |

### Setuid wrappers

NixOS enables a default set of `security.wrappers`. The agent has no password and root is locked, so most of them can do nothing for the agent. Each one is only a way up. `nix/hardening.nix` disables them, then asserts at evaluation time that the enabled set equals an allowlist plus `yolobox.hardening.extraWrappers`.

| Wrapper | Verdict | Why |
|---|---|---|
| `su`, `sg`, `newgrp` | removed | They switch identity by password. Both accounts are locked. No setuid `su` remains. A non-setuid `su` from shadow's store output stays on `PATH`, where it can only fail. |
| `chsh`, `passwd` | removed | They change the account database. `lockRootAndAgent` in `nix/base.nix` calls the store `passwd`, not the wrapper, so the lock on every activation still works. |
| `mount`, `umount` | removed | Setuid mount is a classic escalation (fstab and option parsing). The agent has no reason to mount, and the operator uses `sudo`. |
| `sudo`, `sudoedit` | kept | The operator's way to root. Only `wheel` can exec them (see [nix/base.md](base.md#sudo-is-wheel-only)). |
| `unix_chkpwd` | kept | PAM's password helper. sudo and the login stack call it. |
| `newuidmap`, `newgidmap` | kept, as file capabilities | Rootless podman needs them to map subuids. `newuidmap` carries `cap_setuid,cap_setpcap=ep` and `newgidmap` carries `cap_setgid,cap_setpcap=ep` (nixpkgs adds `cap_setpcap`). Neither is setuid root, so a bug in them gains less. |
| A VPN client's split-tunnel helper | kept per box | Setuid, shipped by the VPN client's module. A box that runs such a client lists the wrapper in `yolobox.hardening.extraWrappers`. It is not in the allowlist because the repo holds generic features and the default box has no VPN client. |

### Sysctls

`systemd`'s `50-coredump.conf` sets `fs.suid_dumpable = 2`, and NixOS renders `boot.kernel.sysctl` into `60-nixos.conf`, which sorts later and wins. That ordering is why `fs.suid_dumpable = 0` takes effect.

| Sysctl | Verdict | Why |
|---|---|---|
| `kernel.kptr_restrict = 2` | blocked | Kernel pointers hidden even from root. nixpkgs sets a `mkDefault` of 1, and a plain assignment at normal priority beats a `mkDefault`, so no `mkForce` is needed. Kernel exploits need addresses, so this costs them a step. |
| `kernel.unprivileged_bpf_disabled = 1` | blocked | Unprivileged eBPF is a frequent source of kernel bugs. Nothing the agent runs needs it. The setting sticks until reboot. |
| `net.core.bpf_jit_harden = 2` | blocked | Blinds constants in JITed BPF for every user, so BPF-based JIT spraying gets harder. Tailscale and the egress table use no unprivileged BPF. |
| `kernel.io_uring_disabled = 2` | blocked | io_uring has been the largest single source of recent local privilege escalations. This is the one setting that costs functionality, so it was decided on its own. The acceptance check proves two things: the agent's `io_uring_setup` call gets `EPERM`, and `claude` and `pi` still start. It does not prove that any particular runtime falls back to another I/O path, so a program that insists on io_uring fails with `EPERM`. A project that needs it re-enables it in `local.nix`; see [How to recognise a breakage](#how-to-recognise-a-breakage). |
| `dev.tty.ldisc_autoload = 0` | blocked | Stops an unprivileged `TIOCSETD` from loading a line discipline module. That path produced several CVEs. |
| `fs.protected_fifos = 2`, `fs.protected_regular = 2` | blocked | Stops following a fifo or regular file that another user owns in a sticky directory such as `/tmp`. This kills a class of `/tmp` races. |
| `fs.suid_dumpable = 0` | blocked | A setuid process never writes a core file. |
| `net.ipv4.conf.{all,default}.accept_redirects`, `secure_redirects`, `send_redirects`, `accept_source_route` = 0 | blocked | ICMP redirects and source routes let a neighbour steer the box's traffic. The box has one gateway and needs neither. |
| `net.ipv6.conf.{all,default}.accept_redirects`, `accept_source_route` = 0 | blocked | The same for IPv6. |
| `kernel.dmesg_restrict` | kept as the kernel config default | The kernel config already sets it, so repeating it adds a second spelling. |
| `kernel.yama.ptrace_scope = 0` | kept | A CONSTITUTION rule keeps it. `py-spy` and `strace -p` on an agent process must work, and the boundary is the uid split, not Yama. See [nix/base.md](base.md#same-uid-ptrace). |
| Unprivileged user namespaces | kept | The chromium sandbox, rootless podman and the nix build sandbox all create unprivileged user namespaces. Turning them off breaks the box's core workload. The price is the residual in [Known residual](#known-residual-the-surface-behind-user-namespaces). |
| `net.ipv4.ping_group_range` | kept | The nixos-lima image sets it. Unprivileged ping is the first network diagnostic. |
| `net.ipv4.ip_unprivileged_port_start = 1024` | blocked | The nixos-lima image sets it to 0, so the agent may bind any port. Whenever sshd is not holding port 22 (during a restart, or after it crashed), the agent binds `0.0.0.0:22` before it returns. Lima and passt forward the host's port to the guest's 22, and `yo`'s initrd probes pin no host key, so `yo up` would talk to the agent's listener, read a server that refuses the operator as the initrd's sshd, and send the LUKS passphrase to it. The floor is set with `lib.mkForce` in a statement of its own, because the image's assignment has the same priority as a plain one and the module system would refuse two. The cost is that an agent dev server on a port below 1024 needs a port of 1024 or above, or a wrapper run by the operator. The acceptance check reads the value back. |
| `allowPing` off | rejected | Only the host reaches `enp0s1`, and ping is the first thing to try when a network breaks. Nothing outside the host could use it. |

### Kernel parameters

`security.protectKernelImage = true` adds the kernel parameter `nohibernate` (a hibernation image would write kernel memory to disk) and sets the sysctl `kernel.kexec_load_disabled = 1` (no replacing the running kernel). `boot.kernelParams` adds the three below.

| Parameter | Verdict | Why |
|---|---|---|
| `slab_nomerge` | blocked | Slab caches stay separate, so a heap overflow in one object type cannot reach another type's memory. |
| `page_alloc.shuffle=1` | blocked | Randomises the free list order, so heap layout is harder to predict. |
| `debugfs=off` | removed | Removes `/sys/kernel/debug`, an interface made for kernel developers. |
| `init_on_alloc=1`, `randomize_kstack_offset` | kept as the kernel config default | nixpkgs `common-config.nix` already enables both. They are not repeated. |
| `audit=0`, `initcall_blacklist`, `psi=0` | not adopted | The article uses them to save RAM or CPU. They do not shrink attack surface, and `audit` would hide events we may want. |
| Transparent huge pages off | not adopted | A memory-use change. No security effect. |
| `zram` swap | not adopted | A RAM trick. See the memory notes in [nix/base.md](base.md#memory-pressure). |
| Closure trims (man pages, fonts, docs) | not adopted | They save disk. Man pages and fonts also serve the agent and the browsers. |
| The article's vsock and virtiofs block | not adopted | Lima and rosetta need both. See the module table above. |

### Known residual: the surface behind user namespaces

The block list cannot close this, and nothing else in the module does either.

With unprivileged user namespaces on, which a CONSTITUTION rule keeps, the agent runs `unshare -Urn` and becomes root inside a new user and network namespace. As that namespace's root it may configure netfilter and traffic control there, so the kernel autoloads `nf_tables` and the net/sched modules (`cls_*`, `sch_*`) for it. Bugs in that code have given real root before: CVE-2024-1086 and CVE-2023-32233 in `nf_tables`, CVE-2022-2588 in the `cls_route` classifier.

`nf_tables` has to stay. The box firewall (the iptables-nft backend) and the bridge networks of rootless podman sit on it. The net/sched modules are a different case. Unless a box configures traffic control, they (`cls_*`, `act_*`, `sch_*`) are present but idle, so blocking them might cost nothing. Nobody has tested that, and a wrong guess breaks tailscale or podman networking, so they stay loadable for now. The mitigation is a current kernel: `nix/base.nix` runs `linuxPackages_latest`, and a rebuild picks up fixes. `TODO.md` holds the open question about the net/sched modules.

### Firewall

The inbound side has no no-regret change left. The input policy is drop, `enp0s1` admits TCP 22 only, `tailscale0` is trusted by rule, and the egress table guards output. What remains is a set of residuals that this module cannot fix, and each points at `TODO.md` or another story file.

| Feature | Verdict | Why |
|---|---|---|
| `services.tailscale.openFirewall` | closed | It would open UDP 41641 on every interface, `enp0s1` included, and a CONSTITUTION rule keeps `enp0s1` at port 22 only. Lima never mirrors guest UDP (see [lima/yolobox.md](../lima/yolobox.md#guest-udp-is-never-mirrored)), so the host is the only possible sender of that UDP, and the host attacking itself is outside the threat model; the rule is kept because a port nobody needs open is not worth explaining. The cost side is unmeasured: direct tailnet paths might depend on the open port, and the fallback is a relay. See [nix/tailscale.md](tailscale.md#firewall). |
| `nixos-fw` input policy drop | kept | Nothing else is opened by default. |
| `enp0s1` admits TCP 22 only | kept | A CONSTITUTION rule keeps it. Lima's start-up wait dials it. See [nix/tailscale.md](tailscale.md#sshd-on-two-interfaces). |
| `tailscale0` trusted by rule | kept | A CONSTITUTION rule keeps it: the tailnet ACL, not the box, decides who reaches a service. See [nix/tailscale.md](tailscale.md#firewall). |
| The egress table | kept | It guards output, not input. See [nix/egress.md](egress.md). |
| `--netfilter-mode=off` | kept | `nixos-fw` is the only writer of rules. See [nix/tailscale.md](tailscale.md#firewall). |
| Switching to the nftables firewall backend | rejected | The egress table and its tests target the iptables-nft backend. A switch changes both for no reduction in exposure. |

Three open problems sit outside this module's reach, so they get pointers and no fix.

- The port-mirror squat (see `TODO.md`). Lima mirrors guest TCP ports 1024-49151 onto the host, so an agent can bind a port before a host tool does. The fix is a change to `lima/yolobox.yaml`, which lima reads only when it creates an instance.
- `AF_VSOCK` from the guest to the host. The egress table does not see it, and today nothing listens host-side. See [nix/egress.md](egress.md#residuals).
- The `198.18.0.0/15` fake-IP range, which the egress table deliberately does not block. See [nix/egress.md](egress.md#residuals).

## Running the acceptance test

Two scripts check that the hardening holds and that nothing the agent needs broke. They run by hand, not under `python3 -m unittest`, because they need a real box.

- `tests/box-hardening-check.sh` runs inside the guest as the operator, which needs passwordless `sudo`. It reads each sysctl and kernel parameter back, tries `socket(AF_BLUETOOTH)` and an SCTP socket as the agent, compares the entries of `/run/wrappers/bin` with the allowlist (plus the names in the `EXTRA_WRAPPERS` environment variable, space separated), calls `io_uring_setup` as the agent and expects `EPERM`, and checks that podman, chromium, `nix build`, `strace -p`, rosetta and the harness binaries (`claude`, `pi`) still work. `ONLY=hardening` or `ONLY=compat` runs one half; unset runs both. The compat half is the one to iterate on against a box that is not hardened yet. It stops at the first broken property and names it.
- `tests/box-hardening-e2e.sh <instance> [EXTRA_WRAPPERS...]` runs on the host and drives the box through `yo`. It starts the instance, runs the guest script, checks `yo status`, round-trips a file with `yo cp`, stops and starts the box (which exercises the LUKS unlock with the new kernel parameters) and runs the guest script again. It passes its remaining arguments to the guest script as `EXTRA_WRAPPERS`, so give it the box's extra wrappers, for example `<helper-name>`, to make the allowlist comparison match. It does not forward `ONLY`.

Run it on a throwaway instance. The test reboots the box and rebuilds it on a branch, which a box you use should not take.

```sh
limactl create --name harden --set '.ssh.localPort=<free port>' lima/yolobox.yaml
yo -m harden bootstrap --version <branch>
tests/box-hardening-e2e.sh harden
limactl delete harden
rm ~/.local/state/yolobox/harden.flake-ref
```

`yo bootstrap --version` builds from a published flake ref, so the branch must be pushed first (see [docs/distribution.md](../docs/distribution.md)). `yo` refuses a `/` in the branch name.

The guest half also runs alone, for example after a normal rebuild of a box:

```sh
yo ssh 'bash -s' < tests/box-hardening-check.sh
```

A box with extra wrappers, or a run of one half only, passes the variables inside the quoted word:

```sh
yo ssh "EXTRA_WRAPPERS='<helper-name>' ONLY=compat bash -s" < tests/box-hardening-check.sh
```
