# yolobox

A development sanbox virtual machine. It's a NixOS Linux run by Lima on a Mac, or by libvirt on a Linux PC.

## To protect against

- An agent that runs `rm -rf ~` or installs malware via `curl2sudo`™.
- A supply chain attack.
- macOS `systempolicyd` write scans, which make builds slow.

It limits what an agent can do. It doesn't lock the agent in.

- The agent runs as its own user with no `sudo`. The filesystem is airgaped. Files move only through git, scp, or rsync.
- The VM can use your 1Password SSH agent. You must approve the access (TouchID or similarly).
- The VM reaches the internet, but not your computer or your local network. To let it reach one address, such as a package cache on your LAN, name it in `/etc/yolobox/local.nix`, for example `yolobox.network.lanAllow = [ "192.168.1.20/32" ];`. On a Linux host this has no effect: the host table rejects the same ranges for the VM, see [docs/linux-host.md](docs/linux-host.md#the-host-table).
- On a Mac, open VM ports from 1024 to 49151 are mapped on the host, except a few login-callback ports such as 8085 (you can open `http://localhost:<port>` directly in your browser). On Linux nothing is mapped on its own: `yo port <guest-port>` opens one.


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

The whole bring-up, on a Mac or on a Linux host once it is set up (below):

```bash
yo bootstrap
yo ssh       # you, the operator
git clone <your dotfiles repo> ~/.dotfiles; ~/.dotfiles/<its install script>
exit
yo enter     # the agent; it lands in the mirror of your current directory
git clone <your dotfiles repo> ~/.dotfiles; ~/.dotfiles/<its install script>
exit
```

`yo bootstrap` creates, unlocks and builds the VM. It offers to add one line at the top of `~/.ssh/config`, above any `Host` or `Match` line, which serves every machine on a Mac and on a Linux host:

```
Include ~/.local/state/yolobox/instances/*/ssh.config ~/.lima/*/ssh.config
```

Both accounts clone over ssh with keys from your 1Password, and the VM already trusts github.com's published host keys, so the first clone asks nothing but 1Password's approval. `yo` itself never clones dotfiles; what they install is yours.

### Linux host

A Linux PC (Fedora, x86_64, with KVM) runs the box under libvirt instead of Lima. The guest and the way you use it are the same. How the host side works, and why, is in [docs/linux-host.md](docs/linux-host.md).

1. In 1Password, create a second `SSH Key` item, Ed25519, titled `yolobox-operator`, in a vault the 1Password agent serves, next to `yolobox-disk`. It is the key `yo` uses to log in to the VM as you, so no private key file is written on the host.
2. Once per host, from a checkout of this repo, set the host up. It installs libvirt and passt, adds a host firewall table and an account `yolobox-vm`, and lets your account manage libvirt. Read [linux/host-setup.md](linux/host-setup.md) first:

   ```bash
   sudo bash linux/host-setup.sh
   ```

3. Install `yo`. With Nix, the package puts `yo` on your `PATH` with its tools, `xorriso` included (it builds the VM's cloud-init disk):

   ```bash
   nix profile install github:aka-rider/yolobox/<version>
   ```

   Or run it from the checkout as `./yo`. Step 2 installed `xorriso`; Python 3.9 or newer, `git`, `ssh`, `curl` and `fzf` must already be on your `PATH`.
4. Bring the VM up as in [Init the VM](#init-the-vm): `yo bootstrap` creates and starts it, then builds it.

   The box gets 16 GiB of memory, a 100 GiB disk and up to 4 CPUs (one less than your machine has, at most 4). To size it, pass the sizes to the `yo bootstrap` that creates it: `yo bootstrap --memory 24 --disk 250 --cpus 3` (memory and disk in GiB; `--cpus` takes 1 to one less than your machine's CPU count). To resize a box that exists, stop it (`yo down`) and run `yo up` with the same flags: memory and CPUs change, a disk only grows. See [Sizing a box](docs/linux-host.md#sizing-a-box).

5. To open a VM port on the host, run `yo port <guest-port>`. It prints the `127.0.0.1` port it chose.

A host whose sshd listens on the tailnet should read the tailnet section of the linux-host story first.

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


`yo --help` gives up to date commands list. `yo help <command>` or `yo <command> --help` explains one command.

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

To add tools you use every day, like `neovim` or `emacs`, put them in `/etc/yolobox/local.nix` inside the VM and rebuild:

```bash
yolobox-guest rebuild switch
```

It rebuilds from the flake ref the last build stamped into `/etc/yolobox/flake-ref`, with the box's own name from `/etc/yolobox/hostname`. For a box built from a release that is the same release; for a box built from a branch it is that branch's current tip, which may have moved since. Use `boot` instead of `switch` to apply the change at the next boot. To move the box to another release, run `yo bootstrap --version <version>` on the host.

Lima reads `lima/yolobox.yaml` only once, when it creates the VM, so editing that file later changes nothing. To give an existing VM more memory, CPUs or disk, stop it and start it with the new size:

```bash
yo down
yo up --memory 24 --cpus 4 --disk 250
```

A flag equal to the box's current size changes nothing, and `yo up` refuses a size change while the box is not stopped.

## Browser and screen

The VM has a virtual screen (`DISPLAY=:0`) and the VM's Chromium, so agents can drive a real browser. `yolobox-screen-record start` and `stop` record the screen into `~/artifacts/`, not into the project, so the recordings don't end up in git.

## Credits

[Lima](https://github.com/lima-vm/lima), [NixOS](https://nixos.org/), [devbox](https://github.com/jetify-com/devbox)
