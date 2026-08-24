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
./yo link                                          # once per repo
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
| python | python@3.12 basedpyright |
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
  git push "ssh://lima-yolobox/home/$(id -un).guest/wrk/yolobox" main

# 5. Build the NixOS configuration inside the VM and reboot. The guest
#    account is declared to mirror whichever host user is bootstrapping it
#    (see flake.nix) — Nix has no pure way to learn that name, so it's
#    passed in impurely.
ssh -F ~/.lima/yolobox/ssh.config lima-yolobox -- \
  "sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild boot --impure --flake ~/wrk/yolobox#yolobox"
limactl restart yolobox
```

After the restart the VM is running the declarative configuration. From now
on, changes to the flake are applied from inside the VM with
`sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake ~/wrk/yolobox#yolobox`
(`yo ssh` already runs from your Mac account, so `$(id -un)` there resolves
to the same name every time).

## Daily use

```bash
# Start or ensure the VM is running
./yo up

# Open a session: ssh plus the herd socket forward and the herd env,
# in the same directory that `code` and `zed` resolve
./yo enter [project]

# Open a plain interactive SSH session, no herd wiring
./yo ssh

# Add a "yolobox" git remote to a host project, mirrored to the same path
# inside the VM. Push from the VM works to the checked-out branch.
./yo link

# Copy the host-only files the VM needs into it: the herdr config (kept if
# one is already there), the ssh config plus every public key, and the
# directory-level git identities from ~/Developer/*/.gitconfig
./yo seed

# Open VS Code Remote-SSH in the VM, at the mirrored dir of the current
# repo (or ~/wrk when outside one). With a project argument, fuzzy-pick
# (fzf) among the VM's git repos under ~/wrk — including projects that
# exist only in the VM.
./yo code [project]

# Open Zed over SSH in the VM, same directory resolution
./yo zed [project]

# Open t3code's web UI, served by the VM, in the Mac's browser
./yo t3

# Print a t3 pairing URL that names this Mac, to open on another machine
./yo pair [base-url]
```

`enter`, `code` and `zed` share one directory resolution: with a project
argument, fuzzy-pick (fzf) among the VM's git repos under `~/wrk`; without
one, the mirrored dir of the host's current repo, or `~/wrk` outside a repo.

`code` and `zed` rely on the `Include ~/.lima/yolobox/ssh.config` line in
`~/.ssh/config`; VS Code also needs the `ms-vscode-remote.remote-ssh`
extension.

`link` is where a new project starts — see [Per-project dev
shells](#per-project-dev-shells) for the bootstrap that follows it.

`t3` runs t3code as a system service inside the VM, bound to the guest's
loopback. `lima/yolobox.yaml` declares one forward for guest port 3773 with
`hostIP: "0.0.0.0"`, so the UI is published on all of the Mac's interfaces
and another machine on the LAN can reach it. Each `./yo t3` checks that the
service is up, mints a fresh pairing token with `t3 pair`, and opens that
pairing URL. The token is what authenticates the browser, so a bookmarked URL
fails with "Authentication required" — run `./yo t3` again instead of reusing
an old link. If the command reports the service is not active, look at
`./yo ssh journalctl -u t3 -n 50`.

`pair` is the same thing for a browser that is not on this Mac. `t3 pair`
builds its URL out of the address the server was started with — `127.0.0.1`,
which means nothing on another machine — so `./yo pair` mints the token with
`t3 auth pairing create --base-url` instead, defaulting to
`http://$(scutil --get LocalHostName).local:3773`. Give it a base URL
argument to name this Mac some other way, e.g. by its VPN address instead of
its LAN name. It prints the URL rather than opening it, because the browser
that needs it is elsewhere.

Two boxes are driven from one browser, not by linking their servers: t3
pairing only ever runs client→server, and no server-to-server pairing exists.
So run `./yo pair` on each Mac, redeem both URLs in the same browser, and
both boxes sit in that UI's environment list — every saved environment is
offered, not only the local one.

That 3773 forward reaches an **existing** VM only after a one-time migration:
lima materialises `lima/yolobox.yaml` into `~/.lima/yolobox/lima.yaml` when it
creates the instance, and from then on `./yo up` starts the instance from that
copy — so editing the repo file changes nothing. The fix is `limactl edit`,
which needs the instance stopped just like the memory bump below:

```bash
limactl stop yolobox
limactl edit yolobox --set '.portForwards = [{"guestPort":3773,"hostIP":"0.0.0.0"},{"proto":"udp","guestPort":68,"guestIP":"0.0.0.0","ignore":true}]'
./yo up
```

The whole array has to be restated, udp/68 rule included — `--set` is yq v4,
and lima disables yq's `load`/`env` operators, so the expression cannot read
`lima/yolobox.yaml` for you.

## Browsers and screen

The VM carries a persistent virtual display (`:0`, 1920x1080) so agents can
drive a real headed browser and take screenshots.

Two Playwright MCP servers are available, one per engine:
`playwright-chromium` and `playwright-firefox`. Both run headed on `:0`.
Each project gets its own persistent browser profile per engine, so logins
survive across sessions of the same project. A second same-engine session in
the same project does not error: Chromium hands the launch off to the
already-running instance, so both sessions drive the same browser. Run one
at a time for predictable control. To guarantee the last writes to a site's
storage persist, end a session with `browser_close` — a killed session can
drop the final write.

Other ways to see or drive the screen:

- `browser_take_screenshot` (MCP) — screenshot of the browser page.
- `maim` — screenshot of the whole virtual display.
- `xdotool` — synthetic keyboard/mouse input.
- `yolobox-screen-record start|stop` — records the whole display to
  `~/artifacts/screen-<ts>.mp4`, with a log next to it.
- `browser_start_video` / `browser_stop_video` (MCP) — records the browser
  tab alone.

All Playwright output — screenshots, PDFs, videos — lands in
`~/artifacts/<project>/`, never inside `~/wrk/<project>`: the push channel
must not see stray binaries in a project's git checkout.

`DISPLAY=:0` is preset in login shells only. In an editor terminal (Zed,
VS Code Remote-SSH), export it yourself before using the display or the
browsers.

Migration note: the MCP server rename (`playwright` →
`playwright-chromium` / `playwright-firefox`) breaks any host-side
permission rule that still matches `mcp__playwright__*`.

The 12GiB memory bump backing this is instance-create-only. An existing
instance needs:

```bash
limactl stop yolobox
limactl edit yolobox --memory 12 --start
```

(`limactl edit` refuses a running instance.)

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

### Python LSP recipe

One chokepoint makes every consumer (Zed, VS Code, opencode, crush, pi,
claude) resolve the same interpreter and server:

1. `devbox.json` packages `python@3.12` and `basedpyright` (a native nix
   build — no glibc hazard).
2. `.envrc` activates devbox, then creates and activates a project `.venv`
   from that python; dependencies are pip-installed into it.
3. `pyrightconfig.json` pins `{"venvPath": ".", "venv": ".venv"}` — every
   basedpyright instance finds the interpreter regardless of which client
   launched it.

`nix/lsp.nix` wires the VM side: Zed's remote server loads direnv
(`load_direnv: "direct"`), and claude auto-loads a basedpyright plugin from
`~/.claude/skills`. opencode and crush read per-project `opencode.json` /
`.crush.json` LSP entries (see PortHub for the reference shape). pi gets it
from pi-lens, an npm extension in the user layer
(`~/.dotfiles/pi/packages.json`), which finds the same `basedpyright` on the
project PATH. VS Code
needs its recommended workspace extensions accepted once per remote
(`.vscode/extensions.json`: basedpyright, ms-python, direnv).

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
`/home/${USER}.guest` (lima-vm/lima#4578) — every VM-side path in this repo
already accounts for the ".guest" suffix.

The guest account name mirrors the host Mac account exactly (lima's cidata
names and uid-matches it that way); there is no fixed "yolobox account" of
its own. An earlier revision hardcoded the account as `xiii` in the flake
— a leftover from a previous Mac account of that name — which silently
diverged from `yo`'s own `$(id -un)`-derived paths once the Mac account
changed, splitting state across two `/home/*.guest` directories with the
same uid. `flake.nix` now threads the real username in via
`YOLOBOX_USERNAME`/`--impure` (see `yo t3`'s switch hint) instead.
