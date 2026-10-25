# Accounts: the operator and the agent

The VM has two accounts so that every AI coding session runs with no path to root. This file explains why there are two, what keeps them apart, and what the split does not protect. The rules that follow from it are in [CONSTITUTION.md](../CONSTITUTION.md).

## Two accounts

Lima creates a guest account named and uid-matched after the Mac account that started the VM. That account is the **operator's**, and only the operator's. `nix/base.nix` declares it as `users.users.${username}` rather than inventing one. `flake.nix` only threads `username` in, and since Nix has no pure way to learn the name, `YOLOBOX_USERNAME=$(id -un)` plus `--impure` carries it in on every `nixos-rebuild`. The operator is the only account in `wheel`, so it is the only one `security.sudo.wheelNeedsPassword = false` covers. It is the account behind `yo ssh`, `yo disk-grow`, `yo bootstrap`, the machine tier of `yo gc`, and any manual `nixos-rebuild`.

Lima >= 2.1.0 names the operator's home `/home/<user>.guest` (it was `/home/<user>.linux` before; lima-vm/lima#4578).

Every AI coding session runs as a second account, `agent`, a constant in `flake.nix` that is never threaded in from the host. Its uid is 1000, its home is `/home/agent`, its only group is `users`, and it is in neither `wheel` nor any sudoers rule. It has no `sudo` at all. `yo enter`, `yo code`, `yo zed` and every t3-spawned session land there.

Three assertions in `nix/base.nix` check `username` at evaluation time, because it reaches sshd config, tmpfiles rules and shell scripts. It must differ from `agent`, it must not be `root`, and it must match `[a-z_][a-z0-9_-]*`. A Mac account that fails one of these stops `nixos-rebuild` with a message naming `YOLOBOX_USERNAME`, instead of building a box that quietly merges the two accounts.

## Why a new account, not a stripped one

The obvious design is to keep one account and take `wheel` away from it. It cannot work, and the reason is one lima behaviour verified directly in the running VM. `lima-init`'s start script runs `usermod -a -G wheel $LIMA_CIDATA_USER` unconditionally on line 22. Only the `useradd` above it is guarded by a conditional. It runs inside a `Type=oneshot` unit with no `Condition=`, and it runs after NixOS activation has already applied that switch's group list. So stripping `wheel` from lima's own account is silently undone at the next boot. The box would look hardened in the config and would not be hardened in fact.

That is why the agent is a brand-new account lima has never heard of. Lima can only re-grant `wheel` to the account it created. The design also fails closed. If anything about the agent account is set up wrong, the agent cannot log in at all, which is loud, instead of landing with root anyway.

The agent's uid is 1000 and not 501 because of an outage this box already lived through. An earlier revision hardcoded `alice`, a former Mac account name, as the box's only guest account. When the Mac account changed, `alice` lived on as a second account with the same uid but its own `/home/alice.guest`, and every module storing state under `config.users.users.alice.home` wrote there instead of the real home. `whoami` and `SUDO_USER` could not show it, because they resolve a uid to whichever passwd entry comes first. Retiring `alice` from every module in one switch was enough, because `nixos-rebuild` removes a user it once declared and no longer does.

uid 501 is lima's own cidata uid, reserved for the operator. Giving `agent` the same uid would reopen exactly that failure, silently splitting state across two homes. So `agent` gets 1000, an ordinary unprivileged uid lima has no opinion about. The comment above `users.users.${agentUser}` in `nix/base.nix` carries the short form of this.

## Locked passwords

`agent` and `root` both carry `hashedPassword = "!"`, and sshd sets `KbdInteractiveAuthentication = false` beside `PasswordAuthentication = false`. A PAM keyboard-interactive prompt therefore cannot turn either account into a password login, and an ssh key is the only way in.

The declaration alone locks nothing on a box whose accounts already exist. With `users.mutableUsers = true` the pinned `update-users-groups.pl` applies `hashedPassword` only when it creates an account, so a hand-run `sudo passwd root` would stick. `system.activationScripts.lockRootAndAgent` in `nix/base.nix` runs `passwd -l` on each account on every activation, and its comment says how it avoids double-locking and why a missing shadow entry fails the activation.

Recognise a regression with `sudo getent shadow root agent`. Both second fields must start with `!`.

## AuthorizedKeysCommand

The first box built from a release (v0.9.1, 2026-09-02) showed the fail-closed design firing. `yo bootstrap` built and rebooted fine and then died at `yo: cannot ssh into yolobox as 'agent'`. The error was `Permission denied (publickey)` and the sshd journal said nothing.

`nix/base.nix` had pointed the agent's `AuthorizedKeysFile` at `/etc/ssh/authorized_keys.d/<operator>`, the file lima-init rewrites every boot, on the theory that a root-owned file is one the agent cannot tamper with. That is true, and it is also why the setup could never work. lima-init writes the file 0600 root inside a 0700 root directory, and sshd opens an `AuthorizedKeysFile` with the target user's uid. So `agent` gets `EACCES` and no key is ever offered a match. `sudo -u agent cat /etc/ssh/authorized_keys.d/<operator>` reproduces it.

The operator never hit this because lima-init also writes the same key into `~/.ssh/authorized_keys`, and sshd's default list checks that first. The agent's own `~/.ssh/authorized_keys` must not be honoured, since the agent can write it and so could mint its own persistent login. The box now reads the root-owned file through an `AuthorizedKeysCommand` run as root. The comment above `environment.etc."ssh/agent-authorized-keys"` explains why the command is a copied `/etc` file and not a store path (sshd's `safe_path` refuses a command whose canonical path has a group-writable component, and `/nix/store` is `1775`).

Recognise the class by a login that fails with `Permission denied (publickey)` for `agent` while the operator logs in fine.

## No journal group

The system journal is the other thing the agent must not read, and it gates on a group. `getfacl` on `/var/log/journal/*/system.journal` shows `group:wheel:r--`, `group:adm:r--`, `other::---`, and the `systemd-journal` group reads it too.

The agent used to carry that group, for one reason. `xvfb` and `openbox` were system units that ran as the agent, so their logs landed in the system journal, and without the group `journalctl -u openbox` silently narrowed to the agent's own messages. The price was too high. The operator's entries (uid 501) live in `system.journal`, not in a journal of their own, and among them are the `sudo: <operator> : ... COMMAND=<argv>` lines for everything the operator ran under `sudo`. An agent in the group could read every command the operator had run as root, arguments included.

So the group is gone, and `xvfb` and `openbox` are the agent's own user units (`systemd.user.services`, `ConditionUser=agent`, wanted by `default.target`; see [nix/display.md](../nix/display.md#display)). Their logs sit in the agent's user journal, which it owns outright without any group. `journalctl --user -u openbox` as the agent is the way to read them. t3code was never part of this. It is a vendor-installed user unit (see [nix/t3.md](../nix/t3.md#vendor-install-and-drop-in)), and its logs sit in the same user journal.

Recognise a regression by `id` as the agent listing anything but `users`, or by `journalctl -u sshd` as the agent printing system entries at all. Without the group it can show only the agent's own messages.

## What the split does not do

The split narrows what an agent can do. It does not isolate or contain it. The agent no longer has root by construction and cannot obtain it without a kernel exploit, which is a real narrowing. Left untouched by it, in plain terms:

- 1Password is reachable by every agent session for as long as the VM runs, not only during one `yo enter`. Any process in the VM can authenticate to GitHub as the operator. See [docs/ssh.md](ssh.md#1password-reaches-the-agent).
- The AWS broker hands out real credentials for every allow-listed SSO profile to any process that asks, by design. See [docs/aws.md](aws.md#design).
- Every commit an agent makes still rides the git push channel back to the Mac, where the operator builds and runs what it wrote.
- Rootless podman's containment rests on the kernel's unprivileged user-namespace support holding, so a kernel exploit reaches exactly as far as it did before.
- Anything an agent leaves under `/home/agent` persists across sessions, with no clean state short of recreating the VM.
- `/etc/yolobox/local.nix` can put `wheel` back in one line, because nothing inside the box enforces the separation at runtime. Only the discipline [CONSTITUTION.md](../CONSTITUTION.md) writes down does.
