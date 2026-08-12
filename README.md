# yolobox v2

A single NixOS VM running on Mac via Lima (Apple Virtualization.framework),
serving as a blast-radius devbox for AI coding agents. 
The VM contains all devtools, packages, harnesses, language and MCP servers, Docker.

It is made to be self-sufficient and the host to have a very limited exposure inside the VM.

## Isolation model

There are zero host files mounts. Git is the only file bridge between macOS and the VM. The VM runs as a single user, and the directory `~/wrk/<project>` inside the VM mirrors the host's project layout.

No private SSH key ever enters the VM.

All host couplings ride a single SSH session:

1. **1Password agent forward** — pushes authenticate through 1Password on the
   host; the VM never sees a private key.
2. **herdr socket RemoteForward** — the host's herdr socket is forwarded
   INTO the VM at `/run/yolobox/herd-host.<pane>.sock`, so in-VM harness
   hooks can report each agent as `yolobox:<agent>` back to the host herd.
3. **Your terminal** — the interactive shell you use to reach the VM.
4. VSCode / Zed remote ssh sessions

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

Per-language toolchains are intentionally **not** system packages. Each
project declares them in its `devbox.json`; devbox, installed VM-wide
from nixpkgs, resolves them against the shared nix store, and direnv
activates the result. This keeps the base VM small and lets each project
pull only the toolchains it actually needs.

### Per-project dev shells

A project opts in with two files, `devbox.json` and `.envrc`. Once per
project, on the host:

```bash
./yo link                                    # once per repo
cp ~/Developer/yolobox/templates/default/{devbox.json,.envrc} .
$EDITOR devbox.json                                # pick packages
printf '.devbox/\n' >> .gitignore
git add devbox.json .envrc .gitignore && git commit -m "add devbox env"
git push yolobox main
```

In the VM, nothing is manual: direnv is whitelisted for everything under
`~/wrk`, so the first `cd ~/wrk/<project>` resolves the packages and writes
`devbox.lock`. The agent commits the lock alongside its work; the host picks
it up with a normal `git pull yolobox main`.

The old dev-shell fragments map to devbox packages roughly as:

| fragment | devbox packages |
| --- | --- |
| rust | cargo rustc rust-analyzer clippy rustfmt |
| python | python@3.13 uv pyright |
| node | nodejs typescript typescript-language-server |
| go | go gopls delve |
| cxx | gcc gnumake cmake pkg-config gdb |
| postgres | `"postgresql": { "version": "latest", "disable_plugin": true }` |

Package names are searchable with `devbox search <name>`, not guaranteed —
treat the table as a starting point. The postgres row disables devbox's
postgresql plugin, which is broken on NixOS (jetify-com/devbox#1559).

#### Migrating a flake-based project

Delete `flake.nix` and `flake.lock`; replace the `.envrc` content with
`eval "$(devbox generate direnv --print-envrc)"`; add a `devbox.json`; swap
`.direnv/` for `.devbox/` in `.gitignore`.

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
`nixos-rebuild switch --flake .#yolobox`. A `switch` that adds a
`systemd.tmpfiles.rules` entry needs `sudo systemd-tmpfiles --create` or
`limactl restart yolobox` after it — see CLAUDE.md.

## Daily use

```bash
# Start or ensure the VM is running
./yo up

# Open a session: ssh plus the herd socket forward and the herd env
./yo enter

# Open a plain interactive SSH session, no herd wiring
./yo ssh

# Add a "yolobox" git remote to a host project, mirrored to the same path
# inside the VM. Push from the VM works to the checked-out branch.
./yo link

# Copy the host's herdr config into the VM, without overwriting it
./yo seed-herdr
```

`link` is where a new project starts — see [Per-project dev
shells](#per-project-dev-shells) for the bootstrap that follows it.

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
missing dynamic linker context. Use the devbox-provided LSPs instead — add
them to the project's `devbox.json` — configured in your helix or zed
settings.

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
