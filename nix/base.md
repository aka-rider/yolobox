# nix/base.nix: the stories behind its settings

`nix/base.nix` carries the short reason beside each setting. This file keeps the incidents and the ways to recognise them. It covers the boot partition, debugger access, memory pressure and disk growth. Two accounts, locked passwords and the journal group are in [docs/accounts.md](../docs/accounts.md#two-accounts). The disk-full incidents are in [docs/disk.md](../docs/disk.md).

## The ESP holds one kernel

`/boot` is the EFI partition itself, `/dev/vda1`, 249 MiB (260,796,416 bytes). It is mounted there to match the shipped nixos-lima image, so a fresh instance never needs a mount migration. The size is baked into the prebuilt image and cannot be grown. vda1 ends at sector 526335 and vda2 starts at 526336, the very next sector, so there is no gap to grow into.

A kernel set is about 92 MiB, and `linuxPackages_latest` brings a new one every few weeks. Two sets fit; three never do. The count that matters is not the retained one. `install-grub.pl` copies every retained generation's kernel into `/boot/kernels` first and unlinks obsolete files only after the `grub.cfg` rename. So the peak during a switch is the retained sets plus every set already there. With `configurationLimit = 2` the next kernel bump holds three sets transiently and dies mid-copy. With `1` the peak is about 198 MB against 260.8 MB.

Lowering the limit frees nothing on the switch that applies it, because pruning runs last. One more switch without a kernel change drains the old set. Verified here: `/boot` went from 198,569,984 bytes used down to 105,672,704.

The failure surfaces late and lies. The closure builds, the profile advances, and only then does the bootloader install die with `No space left on device`. The box keeps running the old generation while `/nix/var/nix/profiles/system` claims the new one. So `readlink /nix/var/nix/profiles/system` and `readlink /run/current-system` disagree, and a reboot comes back on the old one. If the switch was piped through `tail`, the shell reports exit 0. Check `df /boot` first when a switch behaves strangely.

That readlink pair is also what `yo bootstrap` compares to decide whether to reboot (see [docs/distribution.md](../docs/distribution.md#bootstrap-reboot-decision)), and what `nix/guest/yolobox-guest.sh` compares before `nix-collect-garbage -d`, which would delete the running generation after a half-failed switch (see [nix/guest/yolobox-guest.md](guest/yolobox-guest.md#gc-tiers)).

The trade is that no rollback generation survives a kernel bump. That is acceptable, because the VM is rebuildable from this repo.

Moving `/boot` onto the root filesystem was tried (`a2057d8`, reverted in `03e2dbc`). It gives unlimited space, but it makes `nix/base.nix` diverge from the image, so every fresh instance would need a manual mount migration before its first switch.

## Same-uid ptrace

On 2026-10-15 `py-spy dump --pid N` against an already-running agent process died with permission denied. Nothing in the repo set ptrace, so the kernel's Yama default applied: `kernel.yama.ptrace_scope = 1`. That lets a process ptrace only its own descendants, or a process that declared the tracer through `prctl(PR_SET_PTRACER)`, which py-spy cannot arrange from outside. That is why `py-spy record -- python app.py` worked all along (the target is py-spy's child) and attaching to a server started elsewhere did not.

`nix/base.nix` now sets `boot.kernel.sysctl."kernel.yama.ptrace_scope" = 0`. NixOS renders it into `/etc/sysctl.d/60-nixos.conf`, and `systemd-sysctl` carries that file in its restartTriggers, so a `switch` applies it with no reboot.

It is safe because the box's boundary is the uid split (see [docs/accounts.md](../docs/accounts.md#two-accounts)), and Yama does not touch that. The kernel's classic check in `__ptrace_may_access` runs at every scope. It requires the tracer's ids to match the target's real, effective and saved ids, or `CAP_SYS_PTRACE` in the target's user namespace. So at scope 0 the agent (uid 1000) can attach to its own processes (python servers, other claude sessions, herdr, t3). It still cannot attach to the operator's (`systemd-socket-proxyd` for 1Password and the AWS broker, the `sshd-session` holding the reverse forwards) or to root's. What scope 1 protected was one agent-uid process from another, and the agent already owns that uid entirely. It can rewrite `~/.zshrc`, set `LD_PRELOAD`, replace binaries under `~/.local`, and read every secret those processes hold on disk.

The tempting alternative is forbidden by `CONSTITUTION.md`: a `security.wrappers` entry or `setcap` giving py-spy or gdb `cap_sys_ptrace`. That capability bypasses the uid comparison, so the agent could attach to a root process and become root. The only file capabilities on the box today are podman's `newuidmap` and `newgidmap`.

A rootless container is a separate case that scope 0 does not fix. Its processes run under subuids (100069 for the postgres in one stack), which are a different uid to the kernel, so a plain attach is refused at any scope. `podman unshare py-spy dump --pid <host pid>` works. Inside `podman unshare` the agent is root of the rootless user namespace, which holds `CAP_SYS_PTRACE` over every process in it and its children, and it keeps the host pid namespace, so host pids apply. Run it from the agent's home, because `podman unshare` fails to chdir into an unreadable cwd. `podman exec` with py-spy inside the container works too.

Recognise a regression by `sysctl kernel.yama.ptrace_scope` printing `1` again. Two other causes of the same permission denied are not this one: a non-dumpable target (a setuid exec, `PR_SET_DUMPABLE 0`, a credential change after start) and a target with file capabilities the tracer lacks. `strace` is on the box-wide PATH. py-spy and gdb are not; they come from a project's devbox or venv.

## Memory pressure

The VM once had no swap, so memory pressure went straight to the kernel's global OOM killer with no warning stage. On 2026-08-26 a `nixos-rebuild switch` on a box already at about 2.1 GiB available of 12 GiB (ten agent sessions, a five-container podman stack, a Rust link job) tipped it over. The journal shows the kernel killer, not `systemd-oomd`, picking `.chrome-wrapped` and gunicorn workers first, and eventually the uid-501 user manager. `user@501.service` failed with result `signal` and every rootless container died with exit 137. The agents themselves survived, because ssh session scopes hang off `user.slice` directly, not under `user@.service`. So the blast radius of a user-manager kill is exactly the rootless containers.

Two changes address the two halves. `swapDevices` in `nix/base.nix` declares a 16 GiB `/var/swapfile`, so reclaim has somewhere to go before the killer runs, and `lima/yolobox.yaml` raised the box to 16 GiB and 8 cpus. Then systemd's upstream `user@.service` ships `OOMScoreAdjust=100` (verified with `systemctl show user@1000.service -p OOMScoreAdjust`), which makes the per-user manager a preferred victim over every system service at 0. A drop-in (`overrideStrategy = "asDropin"`, so upstream's unit text is kept, not replaced) sets it to -500. Podman sets its own `oom_score_adj` on containers (200 in the incident), so they stay individually killable. What the drop-in changes is that the kernel takes one container worker, or the agent's own link job, before it takes the manager that holds all of them.

## Growing the root disk

`~/.lima/yolobox/disk` is sparse. It reads as the declared ceiling and allocates only what the guest wrote. `lima/yolobox.yaml` declares `disk: 100GiB`, which is the ceiling at creation, and `yo disk-grow` raises it later. The guest mounts `/` with `discard` and `fstrim.timer` runs weekly, so freed blocks are punched back out of the host file. Over-declaring the ceiling costs nothing, which is the intended use.

Raising the ceiling has two halves, and they do different things. `fileSystems."/".autoResize` was already set. It renders `x-systemd.growfs` into `/etc/fstab`, which grows the ext4 filesystem to fill whatever partition it already sits in, so that half was never the gap. The host half is `limactl edit yolobox --disk <GiB>`, grow-only, with the instance stopped. The guest half is `boot.growPartition = true` in `nix/base.nix`. It adds a `growpart` unit that runs before `systemd-growfs-root.service` and grows the partition, vda2, to fill the disk. Its `SuccessExitStatus = "0 1"` makes an already-grown boot a harmless no-op, so it costs nothing on later boots. The partition-growing half was missing for a while, because vda2 only reached 99.7G thanks to nixos-lima's own image config growing it at first boot, before our config took over.

Skipping the guest half is silent. The backing file grows, the guest boots clean, `df` is unchanged, and nothing logs. `systemctl show -p LoadState --value growpart` must answer `loaded`; `not-found` means the running generation cannot grow anything. `growpart` only runs on a boot whose generation carries the option, so the switch must land before the resize. `yo disk-grow` checks this before touching anything.
