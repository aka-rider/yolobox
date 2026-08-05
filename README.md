# yolobox v2

A single NixOS VM running on Mac via Lima (Apple Virtualization.framework),
serving as a blast-radius devbox for AI coding agents. The VM is the trust
boundary: every agent tool, every MCP server, every language toolchain lives
inside it, and the entire system is declared end-to-end by the Nix flake in
this repository. The VM rebuilds itself from this repo via
`nixos-rebuild switch --flake`, meaning no Nix installation is required on
macOS — the host is purely a hypervisor and file bridge.

## Isolation model

There are zero host mounts. Git is the only file bridge between macOS and
the VM. The VM runs as a single user, `xiii`, and the directory
`~/wrk/<project>` inside the VM mirrors the host's project layout.

No private SSH key ever enters the VM. `git push` works through SSH agent
forwarding of the host's 1Password agent, which means unattended pushes are
impossible by design — there is no key material on disk to steal.

Both host couplings ride SSH:

1. **1Password agent forward** — pushes authenticate through 1Password on the
   host; the VM never sees a private key.
2. **Your terminal** — the interactive shell you use to reach the VM.

herdr runs *inside* the VM as its own persistent server (a systemd user
service), with its own herd. Agents started inside the box are foreground
processes of guest panes, so herdr's native process detection recognises
them directly — there is no reporting bridge back to a host herd.

This keeps the blast radius bounded: a compromised agent can only act within
the VM and can only push out through the forwarded 1Password session, which
requires your active unlock.

## Layers

The NixOS configuration is structured into three logical layers, each
expressed as a system module:

- **Base** — zsh, git, helix, sshd, nix-ld, and the essential CLI tools that
  every session needs.
- **Podman** — rootless podman with a docker-compatible CLI and API socket,
  enabling container work without root privileges.
- **Agentic** — the AI coding agents (claude-code, opencode, crush, pi) and
  herdr, all pulled from nixpkgs with a per-harness version-override valve
  so you can pin individual agents without forking the flake. MCP servers
  are declared once in Nix and rendered per-harness automatically.

Per-language toolchains are intentionally **not** system packages. They live
as dev-shell fragments (`rust`, `python`, `node`, `go`, `cxx`, `postgres`)
composed per-project via `yolobox.lib.shell [ ... ]` and activated through
direnv. This keeps the base VM small and lets each project pull only the
toolchains it actually needs.

## Bootstrap

Get the VM running for the first time with these steps:

```bash
# 1. Install Lima via Homebrew
brew install lima

# 2. Start the VM (creates it from the Lima YAML config)
limactl start --name yolobox --yes lima/yolobox.yaml

# 3. One-time in-VM git init (empty repo, updateInstead so a later push
#    can update its checked-out branch directly)
ssh -F ~/.lima/yolobox/ssh.config lima-yolobox -- \
  'mkdir -p ~/wrk/yolobox && git -C ~/wrk/yolobox init -b main && git -C ~/wrk/yolobox config receive.denyCurrentBranch updateInstead'

# 4. From your Mac clone of this repo, push it into the VM
GIT_SSH_COMMAND="ssh -F $HOME/.lima/yolobox/ssh.config" \
  git push ssh://lima-yolobox/home/xiii.guest/wrk/yolobox main

# 5. Build the NixOS configuration inside the VM and reboot
ssh -F ~/.lima/yolobox/ssh.config lima-yolobox -- \
  'cd ~/wrk/yolobox && sudo nixos-rebuild boot --flake .#yolobox'
limactl restart yolobox
```

After the restart the VM is running the declarative configuration. From now
on, changes to the flake are applied with
`nixos-rebuild switch --flake .#yolobox`.

`switch` does not re-run `systemd-tmpfiles-setup.service` — it only runs at
boot, and refuses a manual restart. Any `"C"` (copy-if-absent) tmpfiles rule,
such as the one seeding `~/.pi/agent/settings.json`, keeps the file it
already copied even after a `switch` that changes the seeded content. Pick
up the change with either a real reboot (`limactl restart yolobox`) or
`sudo systemd-tmpfiles --create` inside the VM.

## Daily use

```bash
# Start or ensure the VM is running
./yolobox2 up

# Open an interactive herd session inside the box — the herdr TUI, with
# agents running as guest panes
./yolobox2 enter

# Open a plain SSH session instead — one-shot commands, scripting, git
./yolobox2 ssh

# Add a "yolobox" git remote to a host project, mirrored to the same path
# inside the VM. Push from the VM works to the checked-out branch.
./yolobox2 link

# Copy the host's herdr config into the VM, without overwriting it
./yolobox2 seed-herdr
```

`enter` reaches the same guest herd by two different routes, picked
automatically. From inside a host herdr session it runs `herdr --remote`: the
host client attaches the *guest* server and streams the guest UI into the
pane, with `--remote-keybindings server` negotiating keys so the guest side
drives. From a plain terminal it runs `ssh -t … herdr`: a guest client
attaches that same guest server directly — no nesting, no keybinding
conflict. Either route lands in the same session; detach and re-enter by
either one and the workspace and its agents are still there. A raw
`ssh lima-yolobox` (or `./yolobox2 ssh`) is deliberately a plain shell, not a
herd entry point. The `herdr --remote` route has one-time host setup; the
plain-terminal route needs none of it.

### `enter` prerequisites

`herdr --remote` shells out to plain `ssh <target>` against your real
`~/.ssh/config` — it never sees lima's `-F` flag, so the box's ssh config has
to be pulled in there instead. Add this at the **top** of `~/.ssh/config`,
above the existing `Host *` (which already supplies the 1Password
`IdentityAgent`; `ForwardAgent` is the missing piece), as a one-time host
setup:

```
Include ~/.lima/yolobox/ssh.config
Host lima-yolobox
  ForwardAgent yes
```

lima's `ssh.config` is entirely inside its own `Host lima-yolobox` block, so
this `Include` is host-scoped and cannot affect any other ssh target.

Run from inside a host herdr pane, `enter` attaches through
`herdr --remote`, which the host binary refuses as a nested launch. Enable it
in the **host** `~/.config/herdr/config.toml` — the guard is client-side, so
the guest's config has no say:

```toml
[experimental]
allow_nested = true
```

Host and guest herdr binaries must stay on matching versions. On drift,
`herdr --remote` installs its own binary into the guest's `~/.local/bin`,
shadowing the nix-pinned one — bump `yolobox.harness.herdr.version`/`hash`
in `nix/harnesses.nix` (or upgrade the host binary) to fix it.

Both sides deliberately share `prefix = "ctrl+a"` — different leader keys
would break muscle memory — so escape the outer host session with a doubled
prefix (`ctrl+a ctrl+a`) rather than a different key. `enter` passes
`--remote-keybindings server` so your keys drive the guest session, not the
host one.

Agent detection inside the box is herdr's own native process detection. A
systemd user unit runs `herdr integration install` at boot for claude, pi,
opencode, and codex, best-effort per harness. claude and pi install
immediately; opencode and codex only succeed once that tool has been run at
least once and created its config directory (`~/.config/opencode`,
`~/.codex`) — until then the unit logs "config directory not found" for
that harness and moves on. crush has no upstream herdr integration and falls
back to screen detection only.

## Backups and snapshots

The vz backend used by Lima has **no snapshot support**. Uncommitted work
exists only on the VM disk and is lost if the disk is destroyed. Mitigate
this by pushing to your Mac or to your git remotes regularly — from the VM
side, use `git push` to your origin, or have the Mac pull from the VM's
working tree. Treat the VM as rebuildable cattle and the git remotes as the
durable state. If you need a point-in-time copy of the disk, export it
manually before any destructive operation.

## Editors

**Zed over SSH** works well because its remote server is a static musl
binary. However, Zed-downloaded language servers do **not** work on NixOS:
Zed strips the environment when spawning them, and nix-ld cannot rescue the
missing dynamic linker context. Use the dev-shell-provided LSPs instead,
configured in your helix or zed settings.

**VS Code Remote-SSH** works through nix-ld, which allows the VS Code server
and its extensions to resolve NixOS dynamic libraries correctly. If VS Code
Remote ever misbehaves under nix-ld, the
[nixos-vscode-server](https://github.com/nix-community/nixos-vscode-server)
module is the fallback.

## History

The Docker/bash yolobox v1 lives at commit `4c154fc`. The v2 architecture
replaces that entire stack with a declarative NixOS VM, eliminating the
manual bootstrap scripts and fragile Docker networking.

limactl version: `limactl version 2.2.0` (bring-up 2026-08-05). Note: lima
>=2.1.0 renamed the guest home from `/home/${USER}.linux` to
`/home/${USER}.guest` (lima-vm/lima#4578), so the guest home here is
`/home/xiii.guest` — a ".guest" suffix on the cidata home dir, unrelated to
the account name `xiii` — every VM-side path in this repo already accounts
for it.
