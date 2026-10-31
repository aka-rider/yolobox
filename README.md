# yolobox

A development sanbox virtual machine. It's a NixOS Linux run by Lima.

## To protect against

- An agent that runs `rm -rf ~` or installs malware via `curl2sudo`™.
- A supply chain attack.
- macOS `systempolicyd` write scans, which make builds slow.

It limits what an agent can do. It doesn't lock the agent in.

- The agent runs as its own user with no `sudo`. The filesystem is airgaped. Files move only through git, scp, or rsync.
- The VM can use your 1Password SSH agent. You must approve the access (TouchID or similarly).
- The VM reaches the internet, but not your computer or your local network. To let it reach one address, such as a package cache on your LAN, name it in `/etc/yolobox/local.nix`, for example `yolobox.network.lanAllow = [ "192.168.1.20/32" ];`.
- Open VM ports from 1024 to 49151 are mapped on the host, except a few login-callback ports such as 8085 (you can open `http://localhost:<port>` directly in your browser)


## Quickstart

You need [1Password](https://1password.com/) with its SSH agent turned on. It holds your keys.

Create an `SSH Key` item in 1Password, type Ed25519, titled `yolobox-disk`. It unlocks the VM's disk: `yo up` asks 1Password for it (Touch ID) every time the VM starts. Approve that prompt only when you ran `yo` yourself. Keep the item: if it is lost, the disk is lost.

### Homebrew

```bash
brew install aka-rider/tap/yolobox
```

### Nix

```bash
nix run github:aka-rider/yolobox/<version>
```

### Init the VM

```bash
yo bootstrap
```

Then add this line to `~/.ssh/config`:

```
Include ~/.lima/yolobox/ssh.config
```

## Daily use

The VM has two users. You are the operator. You have the same name as on the host, and you have `sudo`. The agents run as `agent`, which has no `sudo`.

- `yo enter` opens a shell as `agent`. Use it every day.
- `yo ssh` opens a shell as you, for maintenance.
- `yo code [proj]` to the project on the VM with [Visual Studio Code](https://code.visualstudio.com/) (via ssh).
- `yo zed [proj]` same but for [Zed](https://zed.dev)

`yo enter`, `yo code` and `yo zed` try to match the directory you're in.

```bash
cd ~/code/project && yo enter
```

puts you in `~/code/project` in the VM, if the VM has it.

`yo enter <proj>`, `yo zed <proj>`, and `yo code <proj>` open a project in the VM.
They treat a directory with `.git` in it as a project. Fuzzy search works.


`yo --help` gives up to date commands list.

### Copying files

`yo cp` works like `scp`. A leading `:` means the VM.

```sh
cd <project> && yo cp README.md :  # host to VM, same directory
yo cp :notes.md ~/Desktop/         # VM to host
yo cp a.txt b.txt :dump/           # several files into one VM directory
```

For a file whose name starts with `-`, write `yo cp -- -odd.txt :`.

### herdr

[herdr](https://herdr.dev/) runs inside the VM, and `yo bootstrap` adds it to herdr on your host as the `yolobox` machine.

Run `claude` or `pi` in a workspace on that machine. It's the only place herdr can see them. An agent started from `yo enter`, `yo ssh`, `yo code` or `yo zed` is invisible to herdr.

You can paste images from the host's clipboard into a herdr pane in the VM.

### 1Password and AWS

1Password's SSH agent works in the VM automatically.

The VM's disk is encrypted. `yo up` asks 1Password for the `yolobox-disk` key to unlock it.

For AWS, log in on the host, then pick the profile in the VM:

```bash
# on the host
aws sso login --profile <profile>

# in the VM
export AWS_PROFILE=<profile>
aws sts get-caller-identity
```

### Tailscale (optional)

To reach the VM from anywhere on your [Tailscale](https://tailscale.com/) network, log it in once:

```bash
yo ssh sudo tailscale up
```

Tailscale is optional. Without it nothing changes, and `yo status` just says the VM is not logged in.

Once the VM is on a tailnet, every service in it is reachable at its tailnet address (`yo ssh tailscale ip -4`), by whatever the tailnet's access policy admits. Without a tailnet, make the service listen on `0.0.0.0` and open `localhost:<port>` on the host (ports 1024 to 49151 only, minus a few login-callback ports).

### Devbox

Each project gets its tools from [devbox](https://www.jetify.com/devbox). Inside the VM:

```bash
devbox init
devbox add typescript bun nodejs
devbox run -- bun --version
devbox shell   # or work inside it
```

Devbox is entered explicitly, with `devbox shell` or `devbox run`. Nothing loads a project's tools just because you changed into its directory. The editors' language servers pick up the project's tools through devbox on their own.

`devbox search <name>` finds packages.

Zed's own language servers don't run on NixOS. Add them explicitly to the project's `devbox.json` instead.

## Make it yours

Fork it and change whatever you like.

To add tools you use every day, like `helix` or `neovim`, put them in `/etc/yolobox/local.nix` inside the VM and rebuild:

```bash
sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake 'github:aka-rider/yolobox/v<version>#yolobox'
```

Lima reads `lima/yolobox.yaml` only once, when it creates the VM, so editing that file later changes nothing. To give an existing VM more memory or CPUs:

```bash
limactl stop yolobox && limactl edit yolobox --memory 16 --cpus 8 --start
```

## Browser and screen

The VM has a virtual screen, so agents can drive a real browser and take screenshots. Claude does it with the Playwright plugin, and pi with `agent-browser`. Both use the VM's Chromium.

Screenshots, PDFs and videos go to `~/artifacts/`, not into the project, so they don't end up in git. `yolobox-screen-record start` and `stop` record the screen to the same place.

## Credits

[Lima](https://github.com/lima-vm/lima), [NixOS](https://nixos.org/), [devbox](https://github.com/jetify-com/devbox)
