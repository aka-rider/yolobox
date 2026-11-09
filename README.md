# yolobox

A development sandbox for coding agents. It is one NixOS virtual machine, run by Lima on a Mac or by libvirt on a Linux PC. The agent works inside it as an account with no `sudo`, the VM mounts nothing from your computer, and the only ways files move are git and `yo cp`.

It protects against:

- an agent that runs `rm -rf ~` or pipes a script from the internet into `sudo`;
- a supply chain attack in a package the agent installs;
- macOS `systempolicyd` scanning every build write, which makes builds slow.

It limits what an agent can do. It does not lock the agent in: the VM reaches the internet, just not your computer or your local network.

## What you need

On a Mac: [Lima](https://lima-vm.io/) and `fzf` (`brew install lima fzf`), and [1Password](https://1password.com/) with its SSH agent turned on.

On a Linux PC: Fedora on x86_64 with KVM, 1Password with its SSH agent, Python 3.9 or newer, `git`, `ssh` and `fzf`. The host setup script below installs libvirt, passt and `xorriso`.

In 1Password create an `SSH Key` item, type Ed25519, titled `yolobox-disk`. It unlocks the VM's encrypted disk. Put it in a vault of its own that the 1Password SSH agent serves: the VM can ask the forwarded 1Password agent for a signature with any key the agent serves, and one approved `yolobox-disk` signature is the disk passphrase. Keep the item safe. The disk has no recovery key, so losing the item loses the disk.

On a Linux PC also create a second Ed25519 `SSH Key` item titled `yolobox-operator`, in a vault the 1Password agent serves. `yo` logs in to the VM with it, so no private key file is ever written on the host. Then, once per host, from a checkout of this repo:

```bash
sudo bash linux/host-setup.sh
```

It installs libvirt and passt, adds a host firewall table that keeps the VM off your LAN, creates the account `yolobox-vm` that runs the VM, and lets your account manage libvirt.

## Install

```bash
brew install aka-rider/tap/yolobox                    # Mac
nix profile install github:aka-rider/yolobox/v<version>   # Mac or Linux, with nix
```

Or run it once without installing: `nix run github:aka-rider/yolobox/v<version> -- bootstrap`. Always name a release; `yo bootstrap` builds the VM from the same release as the `yo` that runs it.

## Bring-up

```bash
yo bootstrap   # creates, unlocks and builds the VM
yo ssh         # you, the operator
git clone <your dotfiles repo> ~/.dotfiles && ~/.dotfiles/<its install script>
exit
yo enter       # the agent; it lands in the mirror of your current directory
git clone <your dotfiles repo> ~/.dotfiles && ~/.dotfiles/<its install script>
exit
```

`yo bootstrap` offers to add one line at the top of `~/.ssh/config`, above any `Host` or `Match` line. It serves every machine on both kinds of host:

```
Include ~/.local/state/yolobox/instances/*/ssh.config ~/.lima/*/ssh.config
```

The first bootstrap encrypts the disk in place. That can take many minutes on a large disk, and the first few must not be interrupted. If it is interrupted anyway, run `yo destroy --yes && yo bootstrap`.

The box gets 16 GiB of memory, a 100 GiB disk and one CPU less than your machine has, at most 4. Sizes apply only when a box is created: `yo bootstrap --memory 24 --disk 250 --cpus 3`. Later, `yo disk-grow <GiB>` grows the disk; any other size change means `yo destroy --yes` and a new bootstrap.

## Daily use

The VM has two accounts. You are the operator: same name as on the host, with `sudo`. Agents run as `agent`, which has no `sudo`.

| Command | What it does |
|---|---|
| `yo enter [proj]` | a shell as the agent, in the mirror of your current directory or in a project |
| `yo ssh [cmd...]` | a shell or one command as the operator, for maintenance |
| `yo code [proj]`, `yo zed [proj]` | VS Code Remote-SSH or Zed, as the agent |
| `yo cp SRC... DST` | copy like `scp`; a leading `:` means the VM |
| `yo up`, `yo down` | start and unlock the VM, stop it |
| `yo status` | describe the VM; exits 1 when any check warns |
| `yo gc [--deep] [--yes]` | report disk usage; `--yes` reclaims caches, `--deep` also build output |
| `yo disk-grow <GiB>` | grow the disk |
| `yo port <guest-port>` | Linux hosts only: open a VM port on the host's `127.0.0.1` |
| `yo aws-check` | check every allow-listed AWS profile from inside the VM |
| `yo destroy [--yes]` | list, or with `--yes` delete, the VM and everything on it |
| `yo -m NAME <command>` | drive a second machine instead of `yolobox` |

`yo help <command>` explains one command. `yo enter <proj>` picks among the VM's git repositories with fuzzy search.

```sh
cd <project> && yo cp README.md :   # host to VM, same directory
yo cp :notes.md ~/Desktop/          # VM to host
yo cp -- -odd.txt :                 # a name that starts with -
```

`yo cp` never uploads a symlink, and a download never lands `.git`, `.envrc`, `.vscode` or similar control files on the host: the agent wrote them, and your tools would run them as you. A download also skips `node_modules` and `.venv`, and `target` and `.next` where a `Cargo.toml` or `package.json` sits beside them; it names each skipped path. A `target` with no `Cargo.toml` may hold code, so it is copied.

`yo ssh`, `yo enter`, `yo cp`, `yo code` and `yo zed` still run when the 1Password and AWS forwards cannot be set up; they print a warning. `yo ssh` is the way into a box whose forwards are broken.

On a Mac, VM ports 1024 to 49151 are mirrored onto the host's `localhost`, except a few fixed login-callback ports such as 8085. A service listening on `127.0.0.1` or on all addresses in the VM is mirrored. On Linux nothing is mapped until `yo port` opens it.

## Git is the bridge

Add the VM as a git remote over ssh. The host alias is `lima-yolobox`, or `lima-NAME` for `yo -m NAME`, on both kinds of host: `git remote add box agent@lima-yolobox:code/project` reaches the agent's checkout of `~/code/project`. Every checkout in the VM accepts pushes into its current branch, but only while its worktree is clean. The VM commits `devbox.lock`, so pull before you push again. Review what the agent committed before you run any of it on the host.

## Secrets

1Password's SSH agent and the AWS credentials reach the VM over reverse unix sockets. Nothing secret is copied in. 1Password asks for approval per application, not per signature, and an approval can cover later signatures from the same application until 1Password locks. Approve a `yolobox-disk` prompt only while you yourself run `yo up`, `yo bootstrap` or `yo disk-grow`. `yo` asks only when the box's initrd asks for the passphrase: a box whose disk is not encrypted yet (a fresh one) boots without a prompt, and its bootstrap asks once, for the passphrase the disk is encrypted under. Before `yo up` starts a stopped box it checks, without a prompt, that 1Password is running and serves `yolobox-disk`, and otherwise refuses with the box still off.

For AWS, log in on the host and pick the profile per process in the VM:

```bash
aws sso login --profile <profile>                             # on the host
AWS_PROFILE=<profile> aws sts get-caller-identity             # in the VM
```

Copies of the VM's disk image made before its first bootstrap, such as Time Machine backups or APFS snapshots, still hold the unencrypted disk.

## herdr, Tailscale, devbox

[herdr](https://herdr.dev/) runs inside the VM, and `yo bootstrap` adds it to herdr on your host as a machine. Run `claude` or `pi` in a workspace on that machine; an agent started from `yo enter`, `yo ssh` or an editor is invisible to herdr. When `claude update` prints a "Fix" line about `~/.local/bin/claude`, ignore it: that file is the box's launcher.

[Tailscale](https://tailscale.com/) is optional. `yo ssh sudo tailscale up` logs the VM in. Then every port in it is open to whatever your tailnet's access policy admits, so review the policy. ssh from a tailnet peer is refused by design. If `tailscale up` refuses with "requires mentioning all non-default flags", do not pass `--reset`: it clears the flags the box pins. Run `yo ssh sudo tailscale up`, then `yo ssh sudo systemctl restart tailscaled-set`, which applies the flags again.

Each project gets its tools from [devbox](https://www.jetify.com/devbox): `devbox add nodejs`, then `devbox shell` or `devbox run -- <cmd>`. Nothing loads a project's tools because you changed into its directory. Editor language servers run through devbox, so add them to the project's `devbox.json`.

The VM has a virtual screen (`DISPLAY=:0`) and Chromium, so agents can drive a real browser. `yolobox-screen-record start` and `stop` record it into `~/artifacts/`.

## Make it yours

Put extra packages or settings in `/etc/yolobox/local.nix` inside the VM, a NixOS module, and rebuild with `yolobox-guest rebuild switch` (or `boot` for the next boot). The rebuild uses the exact tree the box was last built from. `yo bootstrap --version <release>` moves the box to another release.

`yolobox.swapSizeMiB` sets the swapfile, 16384 by default and 0 for none. The VM's network rules are not configurable. A box that must reach a LAN address can replace the rule file in its `local.nix`: set both `ExecStart` and `ExecReload` of the unit, because a switch reloads it in place.

```nix
systemd.services.yolobox-egress.serviceConfig =
  let load = lib.mkForce "${pkgs.nftables}/bin/nft --file ${./my-egress.nft}";
  in { ExecStart = load; ExecReload = load; };
```

On a Linux host the host table still keeps the VM off the LAN.

## Upgrading

A new `yo` is used with a box it built itself. When `yo status` reports that the box's version differs, run `yo bootstrap`. A `yo` run from a checkout has no version and reports no drift. A major release recreates the box: push your work out, then `yo destroy --yes && yo bootstrap`. The next one, v2, must: its `yo` offers the passphrase only to an initrd whose banner says `yolobox-initrd`, and a box built before it has an initrd that does not. Such a box keeps running until it stops; after that, v2's `yo up` takes its initrd for the seed image's sshd, asks it for your login alone for 600 s, and then ends with:

```
yo: yolobox did not let the operator in within 600 s of lima reporting it Running; the last attempt on 127.0.0.1:60022: its unmarked sshd (the seed image's stage 2) sent its banner and was asked for the operator: refused (ssh said: you@127.0.0.1: Permission denied (publickey).) — an unmarked sshd that keeps refusing the operator is a seed image whose key never landed; recreate it: yo destroy --yes && yo bootstrap
```

So recreate it before you upgrade, while it still runs and you can push your work out. Your data lives in git, not in the box.

## When something breaks

- **DNS fails in the VM.** A full disk shows up as DNS failure first, because dhcpcd cannot write `resolv.conf`. Run `yo gc --yes` from the host, then `yo gc --deep --yes`.
- **`yo` refuses a box it did not make, or the two do not understand each other.** Push your work out of the VM, then `yo destroy --yes && yo bootstrap`. A box is disposable; only the latest `yo` and box are supported together.
- **The VM has no network at all after a boot.** Its egress table failed to load, and without the table the box takes no address. Recreate the box.
- **No network in the VM while the host has it.** A full-tunnel VPN on the host blackholes the VM's traffic, and a fake-IP proxy (Clash, Surge) answers with addresses in `198.18.0.0/15`, which the VM refuses. On a Mac, `limactl stop <name> && yo up` clears Lima flows stuck after a VPN disconnects.
- **`yo up` says the box is not bootstrapped yet.** The box has no version stamp: it is a fresh box from the seed image, with no agent account and no forwards. Run `yo bootstrap`. A bootstrapped box whose agent cannot log in is broken, and `yo up` fails naming `yo bootstrap` too.
- **`yo` says a Lima instance departs from `lima/yolobox.yaml` (Mac).** It names the cause. If `limactl edit` can fix it while the instance is stopped, `yo` prints that one command. A setting Lima reads only at creation means a new instance, which deletes the disk: push your work out, then run the `yo destroy --yes && yo bootstrap` chain it prints. An instance whose port forwards still reverse the 1Password and AWS sockets into the guest departs too (`portForwards.guestSocket`): `yo` forwards those itself, and the printed `limactl edit` removes Lima's.
- **`yo` says another yo is acting on the box.** `yo up`, `yo bootstrap` and `yo disk-grow` say so in one line and wait for that command to finish; `yo down` and `yo destroy --yes` refuse and name themselves to run again.
- **`yo` refuses a machine name as too long.** A unix socket path is shorter than 104 bytes on macOS and 108 on Linux, and the box's sockets live under `$HOME` in a directory named after the machine. Pick a shorter `-m` name.
- **`yo destroy`** keeps the `Include` line and the downloaded base image, which other machines share. It stops the box's own AWS broker first, which lives in the box's directory.

## Limits

- Disk encryption protects the image file at rest, not the running VM: its memory and its unlocked disk are plaintext to whoever controls the host.
- The initrd lives on the unencrypted boot partition and is not signed. Whoever can write the image file can replace it, and the replacement receives the passphrase at your next `yo up`.
- One `yolobox-disk` item is one passphrase for every box: all boxes derive it from the same key and message, so losing or leaking one box's passphrase exposes the others.
- On a Mac, `yo` sends the passphrase only while `lsof` names the instance's own hostagent as the one listener on the box's ssh port, checked right before the login. Another process of your account could still take the port in the moment between that check and ssh's connect.
- A program in the VM can bind a mirrored port before a host tool binds it, and then the host tool fails or talks to the VM. Only a few fixed login-callback ports are held back.
- The box sets `PI_CLIPBOARD_OSC52=1`, so pi copies through the terminal's OSC 52 escape, and any program in a pane can emit the same escape. Whether it reaches your host clipboard depends on your terminal.
- `yo code` and `yo zed` make your editor install and run a server in the agent's home, which the agent can replace.
- The `narHash` that `yo bootstrap` pins comes from the box's own first fetch of the release, so it holds later fetches to that tree and does not vouch for the first.

## Credits

[Lima](https://github.com/lima-vm/lima), [NixOS](https://nixos.org/), [devbox](https://github.com/jetify-com/devbox), [herdr](https://herdr.dev/).
