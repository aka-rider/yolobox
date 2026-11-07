# Hardening

The code is `nix/hardening.nix`. It shrinks what an agent session can reach in the guest's kernel and in its setuid binaries, and it keeps everything the box needs to work. The idea comes from [a lowendspirit article](https://lowendspirit.com/discussion/11543/) that trims a NixOS VPS by blacklisting modules, dropping services and adding kernel parameters. That article aims at RAM. This module repeats the exercise aimed at attack surface, and the table below ([The journal](#the-journal)) records every feature we looked at, the verdict and the reason. The comments in the `.nix` file give the short reason for each line. This file holds the full list, so the next person does not redo the inventory.

The threat is the agent. It runs as uid 1000 with no `sudo` (see [docs/accounts.md](../docs/accounts.md#two-accounts)), so the way to root is a kernel bug or a setuid binary. The kernel loads a protocol module on demand when any process opens a socket of that family, which means the agent can pull `bluetooth`, `sctp` or `tipc` into the running kernel with one `socket()` call, and a bug in any of them becomes reachable. Many network-protocol kernel CVEs arrive that way. Blocking the modules the box never uses closes that route for every name on the list. It does not close the class. The modules the box needs stay loadable, and with unprivileged user namespaces on, the agent can still reach some of them as root of its own namespace (see [Known residual](#known-residual-the-surface-behind-user-namespaces)). The setuid wrappers are the second path, and the box keeps only five wrappers: `sudo`, `sudoedit`, `unix_chkpwd`, `newuidmap` and `newgidmap`. The last two carry file capabilities, not the setuid bit.

## How to recognise a breakage

Every item here is a deliberate removal, so a breakage has a known shape. A fix for one box lives in that box's own `/etc/yolobox/local.nix`, because the repo holds generic features (see [docs/distribution.md](../docs/distribution.md#flake-ref-not-a-checkout)). A blocked module is the exception, as its row says.

| Symptom | Which item | Fix |
|---|---|---|
| `socket()` fails with `EAFNOSUPPORT` (address family not supported) or `modprobe` prints nothing and exits 1 for a protocol module | A name in `blockedModules`: the `install <module> .../false` line in `modprobe` config, plus the blacklist | Unblocking a module is a change to this repo, not to `local.nix`. Remove the name from `blockedModules` in `nix/hardening.nix` and add a line to the journal below saying what needs it (a CONSTITUTION rule requires the line). A `lib.mkForce` of `boot.blacklistedKernelModules` or `boot.extraModprobeConfig` in `local.nix` is no fix, because it would also drop nixpkgs' own firewire blacklist and every other modprobe line. |
| A program fails with `EPERM` on `io_uring_setup`, or a database or runtime that uses io_uring runs slower | `kernel.io_uring_disabled = 2` | `boot.kernel.sysctl."kernel.io_uring_disabled" = lib.mkForce 0;` in `local.nix` |
| `nixos-rebuild` stops with "security.wrappers differs from the allowlist" and names an unexpected wrapper | The assertion in `nix/hardening.nix` | If the box needs the wrapper, add it to `yolobox.hardening.extraWrappers` in `local.nix`, with the capabilities it carries: `{ <helper-name> = ""; }` for a setuid-root helper. See [Setuid wrappers](#setuid-wrappers). |
| `nixos-rebuild` stops with "A wrapper carries other privilege than the allowlist states" | The same assertion, per wrapper | The message prints what the wrapper has and what the allowlist wants, for example `have {"capabilities":"cap_setpcap,cap_setuid,cap_sys_ptrace",...}`. Fix the wrapper. If it is an extra one and the privilege is right, state it in `extraWrappers` exactly as printed. |
| `nixos-rebuild` stops with "A wrapper carries a capability the box never allows" | The same module | Not negotiable: no `extraWrappers` entry lifts it. The message lists the names; see [Capabilities no wrapper carries](#capabilities-no-wrapper-carries). |
| `nixos-rebuild` stops with "An extra wrapper wraps a binary of another name" | The same module | The wrapper's `source` must be a binary named like the wrapper. A helper that needs another name gets a wrapper of that name. |
| `nixos-rebuild` fails with "A definition for option `yolobox.hardening.extraWrappers' ... is not of type" | A `local.nix` from before the option became an attribute set | Change the list to a set of name and capabilities, see the migration note under [Setuid wrappers](#setuid-wrappers). |
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
| `pppoe`, `l2tp_ppp`, `pptp` | blocked | The `AF_PPPOX` protocols for PPP over Ethernet, over L2TP and over PPTP (`PX_PROTO_PPTP`, protocol 2 of family 24). The box makes no PPP link. `pptp` is a GRE handler that parses every GRE packet the box receives once loaded. |
| `l2tp_core` | blocked | What `l2tp_ppp` (above), `l2tp_ip` and `l2tp_ip6` sit on. The last two autoload from `socket(AF_INET, SOCK_DGRAM, IPPROTO_L2TP)` (protocol 115) and have a use-after-free history (CVE-2023-4206 and neighbours); without `l2tp_core` none of the three loads. |
| `mctp` | blocked | The `AF_MCTP` socket family (45), a management bus for BMCs. A VM has no such bus. |
| `scsi_transport_iscsi` | blocked | The iSCSI transport, which autoloads from `socket(AF_NETLINK, SOCK_RAW, NETLINK_ISCSI)` (protocol 8) and whose netlink parser gave root to any user (CVE-2021-27365, CVE-2021-27363/4). The root disk is virtio-blk; nothing speaks iSCSI. |
| `xfrm_user` | blocked | The IPsec policy socket, `NETLINK_XFRM` (protocol 6), with its own CVE history (CVE-2024-27404 and earlier). Nothing here uses kernel IPsec; tailscale is WireGuard in userspace, and `af_key` is blocked for the same reason. |
| `vxlan`, `geneve`, `bareudp`, `gtp`, `hsr`, `macsec`, `bonding`, `team`, `ipvlan` | blocked | Link kinds the kernel autoloads as `rtnl-link-<kind>` when `ip link add type <kind>` asks for one, which the agent can do as root of its own user and network namespace (`unshare -Urn ip link add v type vxlan id 1`). Each is a packet parser (tunnels, HSR rings, MACsec, link aggregation) that the box never configures: rootless podman uses `bridge` and `veth`, which stay, and tailscale uses `tun`. `macvlan` is not listed: podman's `macvlan` network driver is a documented use, and the kind is a thin one. |
| `cls_basic`, `cls_bpf`, `cls_cgroup`, `cls_flow`, `cls_flower`, `cls_fw`, `cls_matchall`, `cls_route`, `cls_u32` | blocked | Traffic-control classifiers. Nothing on the box configures traffic control (no `tc` in a unit, no tailscale or podman path that calls it), so they are idle, but the agent can load each one as root of its own user and network namespace with `unshare -Urn tc filter add ...`. `cls_route` has given real root (CVE-2022-2588), and `cls_u32` and `cls_fw` have the same kind of history. |
| `act_bpf`, `act_connmark`, `act_csum`, `act_ct`, `act_ctinfo`, `act_gact`, `act_gate`, `act_ife`, `act_meta_mark`, `act_meta_skbprio`, `act_meta_skbtcindex`, `act_mirred`, `act_mpls`, `act_nat`, `act_pedit`, `act_police`, `act_sample`, `act_simple`, `act_skbedit`, `act_skbmod`, `act_tunnel_key`, `act_vlan` | blocked | Traffic-control actions, the same case: idle on the box, reachable from a namespace, and `act_*` code runs on every packet a classifier matches. `act_ct` and `act_nat` depend on conntrack, not the other way round, so blocking them leaves `nf_conntrack` and the egress table alone. |
| `sch_cake`, `sch_cbs`, `sch_choke`, `sch_codel`, `sch_drr`, `sch_dualpi2`, `sch_etf`, `sch_ets`, `sch_fq`, `sch_fq_pie`, `sch_gred`, `sch_hfsc`, `sch_hhf`, `sch_htb`, `sch_ingress`, `sch_mqprio`, `sch_multiq`, `sch_netem`, `sch_pie`, `sch_plug`, `sch_prio`, `sch_qfq`, `sch_red`, `sch_sfb`, `sch_sfq`, `sch_skbprio`, `sch_taprio`, `sch_tbf`, `sch_teql` | blocked | Every qdisc module except `sch_fq_codel`. Their history is the reason (`sch_qfq` CVE-2023-3611, `sch_hfsc` CVE-2023-4623, `sch_netem`, `sch_sfb`, `sch_tbf` and others), and nothing here shapes traffic. `sch_ingress` also provides `clsact`; the only users are `tc` and BPF tools the box does not run, and the nftables `ingress` hook does not need it. `sch_fq` is blocked with the rest, so a box that sets `net.core.default_qdisc` to anything but `fq_codel` in its `local.nix` gets a failed sysctl write at boot. |
| `sch_fq_codel` | kept | `net.core.default_qdisc` is `fq_codel`, set by systemd's own `50-default.conf`, and every new device gets it: `tailscale0` and the box's NIC show `fq_codel` in `tc qdisc show`. Blocking it would leave devices on the built-in `pfifo_fast` and fail that sysctl on every boot. |
| `mq`, `pfifo`, `bfifo`, `pfifo_fast`, `noqueue` | not listed | Built into the kernel, not modules, so there is nothing to block. |
| `sch_mqprio_lib` | not listed | A library module that only `sch_mqprio` and `sch_taprio` (both blocked) and two DSA switch drivers a VM never has depend on. |
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

NixOS enables a default set of `security.wrappers`. The agent has no password and root is locked, so most of them can do nothing for the agent. Each one is only a way up. `nix/hardening.nix` disables them, then asserts at evaluation time two things. The enabled set must equal an allowlist plus `yolobox.hardening.extraWrappers`, and every enabled wrapper must carry exactly the privilege its entry states: `program` equal to its name, `owner` root, `setgid` off, `source` the binary nixpkgs' own module wraps, and `setuid`, `capabilities`, `group` and `permissions` as in the table below and the paragraph after it. A wrapper that keeps its name but gains `cap_sys_ptrace` on `newuidmap`, a different `program`, a `source` of another package (a `local.nix` line `security.wrappers.unix_chkpwd.source = "${pkgs.bash}/bin/bash"` would hand the agent a setuid shell under a kept name), a setuid bit, or `o+x` on `sudo`, therefore fails the rebuild. Independently of every entry, any enabled wrapper whose capabilities name one of [Capabilities no wrapper carries](#capabilities-no-wrapper-carries) fails it too.

| Wrapper | Verdict | Why |
|---|---|---|
| `su`, `sg`, `newgrp` | removed | They switch identity by password. Both accounts are locked. No setuid `su` remains. A non-setuid `su` from shadow's store output stays on `PATH`, where it can only fail. |
| `chsh`, `passwd` | removed | They change the account database. `lockRootAndAgent` in `nix/base.nix` calls the store `passwd`, not the wrapper, so the lock on every activation still works. |
| `mount`, `umount` | removed | Setuid mount is a classic escalation (fstab and option parsing). The agent has no reason to mount, and the operator uses `sudo`. |
| `sudo`, `sudoedit` | kept | The operator's way to root. Only `wheel` can exec them (see [nix/base.md](base.md#sudo-is-wheel-only)). |
| `unix_chkpwd` | kept | PAM's password helper. sudo and the login stack call it. |
| `newuidmap`, `newgidmap` | kept, as file capabilities | Rootless podman needs them to map subuids. `newuidmap` carries `cap_setuid,cap_setpcap=ep` and `newgidmap` carries `cap_setgid,cap_setpcap=ep` (nixpkgs adds `cap_setpcap`). Neither is setuid root, so a bug in them gains less. |
| A VPN client's split-tunnel helper | kept per box | Setuid, shipped by the VPN client's module. A box that runs such a client lists the wrapper in `yolobox.hardening.extraWrappers`. It is not in the allowlist because the repo holds generic features and the default box has no VPN client. |

The allowlist values come from the pinned nixpkgs: `sudo` and `sudoedit` (`security/sudo.nix`) and `unix_chkpwd` (`security/pam.nix`) are setuid root, and `newuidmap` and `newgidmap` (`programs/shadow.nix`) are root-owned with `cap_setuid+ep` and `cap_setgid+ep`. None is setgid. The expected `source` of each is read from the same option the module reads (`security.sudo.package`, `security.pam.package`, `security.loginDefs.package`), so a package bump moves both sides together and only a `source` pointed elsewhere differs. nixpkgs' wrapper module (`security/wrappers/default.nix`) runs `setcap "cap_setpcap,<capabilities>"`, so a capability wrapper always carries `cap_setpcap` too.

`group` and `permissions` are compared because the `setuid` and `setgid` options are not the whole story. The module builds a capability wrapper (non-empty `capabilities`) with `chmod <permissions>` alone and ignores its `setuid` option, so `permissions = "u+rxs"` on a capability wrapper would make it setuid root while `setuid` still reads `false`. And `permissions` is where `o+x` lives, which decides whether the agent may exec the wrapper at all. Every kept wrapper but two has group `root` and the module default `u+rx,g+x,o+x` (`permissions`' default, `security/wrappers/default.nix`; `unix_chkpwd` in `security/pam.nix`; `newuidmap` and `newgidmap` in `programs/shadow.nix`). `sudo` and `sudoedit` have group `wheel` and `u+rx,g+x`: with `security.sudo.execWheelOnly` (which `nix/base.nix` sets) `security/sudo.nix` defines `group = if cfg.execWheelOnly then "wheel" else "root"` and `permissions = if cfg.execWheelOnly then "u+rx,g+x" else "u+rx,g+x,o+x"`. `nix/hardening.nix` states those two values in its table and does not read the option, so switching `execWheelOnly` off fails the rebuild as well as the CONSTITUTION rule. The strings compare verbatim: an equivalent spelling such as `0750` is refused too, which is the safe direction.

An `extraWrappers` entry has two accepted shapes. Either it is owned by root with group `root` and the default permissions, like the kept ones, or its `permissions` are exactly `u+rx,g+x` and its group is anything. The second shape exists because nixpkgs' `programs.wireshark` defines `dumpcap` with group `wireshark` and `u+rx,g+x` (`programs/wireshark.nix`), a legitimate wrapper that only root and that group can exec. It is never wider than the first shape, which anyone can exec, so its group is taken as given and not compared. Any other `permissions` string, `u+rxs` or `a+rx` or `0755` for instance, fails the rebuild, and the message prints the rendered values. A wrapper that needs an owner other than root, or a mode outside these two, cannot be stated until `extraWrappers` takes a richer value than the capability string. An extra wrapper's `source` is not known to the allowlist, so the rule for it is the basename: the binary must be named like the wrapper (`dumpcap` wraps `.../bin/dumpcap`). Without it, `extraWrappers.dumpcap` with `security.wrappers.dumpcap.source` pointed at a shell would carry dumpcap's capabilities into that shell under an accepted name.

`yolobox.hardening.extraWrappers` is an attribute set from the wrapper's name to the capabilities it must carry: comma separated, in any order, as `getcap` lists them (include `cap_setpcap`, which NixOS adds). `EXTRA_WRAPPERS` in `tests/box-hardening-check.sh` takes the same spelling. The assertion and the check script each sort the value (and drop duplicates) before they compare it, because `getcap` prints in capability-number order (`cap_setpcap,cap_net_admin,cap_net_raw`), not alphabetically. The empty string means a setuid-root wrapper with no capabilities. A non-empty string means a root-owned capability wrapper that is not setuid (nixpkgs makes setuid and capabilities mutually exclusive). So `{ dumpcap = "cap_net_admin,cap_net_raw,cap_setpcap"; splitHelper = ""; }` states one capability wrapper and one setuid one. Naming a wrapper of the allowlist there fails the rebuild, because the table already fixes its privilege. The value is also what the wrapper's `capabilities = "cap_net_admin,cap_net_raw+ep"` must render to; the assertion message prints the rendered form when they differ.

Migration: a `local.nix` that set the list form fails to evaluate with a type error. Turn `extraWrappers = [ "dumpcap" ];` into `extraWrappers = { dumpcap = "cap_net_admin,cap_net_raw,cap_setpcap"; };` (use `getcap /run/wrappers/bin/dumpcap` on the running box to read the capabilities, or the assertion message after a first try) and `[ "helper" ]` into `{ helper = ""; }`.

#### Capabilities no wrapper carries

A wrapper's capabilities land in the agent's process, so each name below is refused on every enabled wrapper, an `extraWrappers` entry included, because it reaches root (or what root owns) in one step. `cap_setuid` and `cap_setgid` are on the list too, except on `newuidmap` and `newgidmap`, the two kept wrappers whose whole purpose is one of them: a third wrapper with `cap_setuid` is `setuid(0)` by another name.

| Capability | Why it is refused |
|---|---|
| `cap_sys_ptrace` | Attaches to root's processes and hands the agent root (CONSTITUTION, [nix/base.md](base.md#same-uid-ptrace)). |
| `cap_sys_admin` | The catch-all: mounts, namespaces, `setns`, device ioctls. |
| `cap_dac_override`, `cap_dac_read_search` | Read or write any file whoever owns it, root's keys included. |
| `cap_sys_module` | Loads a kernel module, which runs as the kernel. |
| `cap_sys_rawio` | Raw access to block devices and ports: the root disk under the filesystem. |
| `cap_setfcap` | Writes file capabilities, so it mints any other capability on any binary. |
| `cap_fowner`, `cap_chown` | Take over any file's mode or owner, root's included. |
| `cap_bpf` | Loads BPF programs that `kernel.unprivileged_bpf_disabled` keeps from the agent. |
| `cap_sys_boot` | Reboots, or `kexec` a kernel of the agent's choosing. |
| `cap_mknod` | Creates device nodes, the root disk's among them. |
| `cap_setuid`, `cap_setgid` outside `newuidmap` and `newgidmap` | Become any uid or gid. The two id-map helpers carry one each, and nothing else needs either. |
### Sysctls

`systemd`'s `50-coredump.conf` sets `fs.suid_dumpable = 2`, and NixOS renders `boot.kernel.sysctl` into `60-nixos.conf`, which sorts later and wins. That ordering is why `fs.suid_dumpable = 0` takes effect.

| Sysctl | Verdict | Why |
|---|---|---|
| `kernel.kptr_restrict = 2` | blocked | Kernel pointers hidden even from root. nixpkgs sets a `mkDefault` of 1, and a plain assignment at normal priority beats a `mkDefault`, so no `mkForce` is needed. Kernel exploits need addresses, so this costs them a step. |
| `kernel.unprivileged_bpf_disabled = 1` | blocked | Unprivileged eBPF is a frequent source of kernel bugs. Nothing the agent runs needs it. The setting sticks until reboot. |
| `net.core.bpf_jit_harden = 2` | blocked | Blinds constants in JITed BPF for every user, so BPF-based JIT spraying gets harder. Tailscale and the egress table use no unprivileged BPF. |
| `kernel.io_uring_disabled = 2` | blocked | io_uring has been the largest single source of recent local privilege escalations. This is the one setting that costs functionality, so it was decided on its own. The acceptance check proves two things: the agent's `io_uring_setup` call gets `EPERM`, and `claude` and `pi` still start. It does not prove that any particular runtime falls back to another I/O path, so a program that insists on io_uring fails with `EPERM`. A project that needs it re-enables it in `local.nix`; see [How to recognise a breakage](#how-to-recognise-a-breakage). |
| `dev.tty.ldisc_autoload = 0` | blocked | Stops an unprivileged `TIOCSETD` from loading a line discipline module. That path produced several CVEs. |
| `dev.tty.legacy_tiocsti = 0` | blocked | `TIOCSTI` pushes bytes into a tty's input queue as if typed. A process that holds a tty's fd (a job the operator left running, a container with the tty passed in) can type into the shell that owns it, which is the classic escape from any sandbox that shares a terminal. With the sysctl at 0 the ioctl needs `CAP_SYS_ADMIN` in the initial namespace. Nothing on the box uses it: tmux, herdr and the harnesses write to the master side of their own ptys. |
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

The block list closes only part of this, and nothing else in the module closes the rest.

With unprivileged user namespaces on, which a CONSTITUTION rule keeps, the agent runs `unshare -Urn` and becomes root inside a new user and network namespace. As that namespace's root it may configure netfilter and traffic control there, so the kernel autoloads `nf_tables` and the net/sched modules (`cls_*`, `sch_*`) for it. Bugs in that code have given real root before: CVE-2024-1086 and CVE-2023-32233 in `nf_tables`, CVE-2022-2588 in the `cls_route` classifier.

`nf_tables` has to stay. The box firewall (the iptables-nft backend) and the bridge networks of rootless podman sit on it. The net/sched modules were a different case, and `nix/hardening.nix` now blocks them: every `cls_*`, `act_*` and `sch_*` module except `sch_fq_codel`. The journal rows under [Kernel modules](#kernel-modules) say why each family is idle. What is left behind user namespaces is `nf_tables` itself, the filesystem `mount(2)` path (see the rejected row under Kernel modules), and the code that is built into the kernel rather than a module. The mitigation for those is a current kernel: `nix/base.nix` runs `linuxPackages_latest`, and a rebuild picks up fixes.

How the net/sched block was checked against what the box really uses. On a running box only `sch_fq_codel` was loaded, and `tc qdisc show` listed `noqueue`, `pfifo_fast` and `fq_codel`, all built in or that module. The `tailscaled` and `podman` binaries contain no qdisc or classifier name, and `systemd-networkd`, the one systemd component that configures qdiscs, is not running (the box uses dhcpcd). `tests/box-hardening-check.sh` proves it on a switched box in both directions: the hardening half has the agent try to attach a `netem` qdisc and a `u32` filter in its own namespace and expects both to fail with no `sch_`, `cls_` or `act_` module loaded afterwards, and the compat half attaches `fq_codel` the same way and runs a container on a bridge network. If a box's own workload needs traffic control, the way out is a rebuild with that module dropped from `blockedModules`, with a journal row saying what needs it.

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

- `tests/box-hardening-check.sh` runs inside the guest as the operator, which needs passwordless `sudo`. It reads each sysctl and kernel parameter back, tries `socket(AF_BLUETOOTH)` and an SCTP socket as the agent, compares the entries of `/run/wrappers/bin` with the allowlist (plus the names in the `EXTRA_WRAPPERS` environment variable, space separated; an entry `name=cap_a,cap_b` also states the capabilities that wrapper must carry, as `getcap` names them sorted, and a bare name means none), calls `io_uring_setup` as the agent and expects `EPERM`, and checks that podman, chromium, `nix build`, `strace -p`, rosetta and the harness binaries (`claude`, `pi`) still work. `ONLY=hardening` or `ONLY=compat` runs one half; unset runs both. The compat half is the one to iterate on against a box that is not hardened yet. It stops at the first broken property and names it.
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
