# Working on yolobox

This file is for whoever changes the repo: where things live, how to check a change, when a change is done, and the facts the code cannot tell you. `CONSTITUTION.md` holds the rules; read it next. `README.md` is the operator's guide.

## The model in one screen

- **Two halves of one tool.** `yo` runs on the host and does everything the host does to the VM. `yolobox-guest` runs in the box and holds the guest-side logic `yo` calls over ssh. A new guest subcommand exists on a box only after the box is rebuilt.
- **Two accounts.** The operator has the host account's name, uid 501 and `wheel`. The agent is the constant account `agent`, uid 1000, with no `sudo`. Every coding session runs as the agent.
- **Two hosts, one guest.** `yo` picks a driver once, from the host OS: Lima (vz) on macOS, libvirt (`qemu:///system`, passt, sVirt) on Linux. The guest copies Lima's address plan on both (gateway 192.168.5.2, guest 192.168.5.15, NIC `enp0s1`) and is never told which OS runs it. A Mac runs its own CPU's guest; a Linux host runs x86_64 only.
- **Egress.** One constant nft table in the guest rejects traffic to the host and to private networks, except DNS to the gateway and DHCP. `tailscale0` is trusted; the tailnet ACL is the gate there. `dhcpcd` and `tailscaled` require the table's unit, so a box whose table fails to load at boot has no address and is unreachable; recreate it. A switch reloads the table in place and never restarts either. A Linux host adds its own table, keyed on the uid `yolobox-vm`, that the guest cannot undo.
- **Git is the bridge.** No host mounts. Checkouts in the VM accept pushes with `receive.denyCurrentBranch = updateInstead`.
- **Encrypted root.** `/dev/vda2` is LUKS2. `yo up` derives the passphrase from the 1Password key `yolobox-disk` and feeds it to an sshd in the initrd. The seed image's first boot of a yolobox generation encrypts the plain root in place.
- **Published flake ref.** `yo bootstrap` builds `github:aka-rider/yolobox/<rev>?narHash=<hash>` resolved from a release, branch or `github:OWNER/REPO/REF`. The box stamps that ref into `/etc/yolobox/flake-ref`, and `yolobox-guest rebuild` builds it again and refuses a stamp that does not start with `github:`. The ref names the full rev, so every new rev is a new system generation, a docs-only commit included. The VM holds no checkout of this repo.
- **Secrets over reverse unix sockets.** 1Password's agent and the AWS broker are forwarded into `/run/yolobox-op/` (operator-owned) and proxied by `systemd-socket-proxyd` to agent-owned sockets in `/run/yolobox/`. Nothing sensitive is copied in.

## Where things live

| Path | Responsibility |
|---|---|
| `yo` | the launcher: imports `yolobox_cli` and calls `cli.entry` |
| `yolobox_cli/` | the host CLI, Python 3.9 and the stdlib only; each module's docstring says what it owns |
| `yolobox_cli/lima.py` | the Mac driver; shipped only to macOS |
| `yolobox_cli/libvirt.py`, `libvirt_host.py` | the Linux driver and its host preflight; shipped only to Linux |
| `yolobox_cli/common.py` | what `aws-broker` shares with `yo`; imports nothing else from the package |
| `aws-broker` | the host's AWS credential broker, started and stopped by `yo` |
| `facts.json` | every literal more than one language reads |
| `flake.nix` | wires the modules and reads the three `YOLOBOX_*` variables |
| `nix/lib/args.nix` | the `yolobox` module argument every module shares, and the facts rendered into `/etc/yolobox/facts.sh` |
| `nix/base.nix` | accounts, filesystems, boot, sshd, git, the socket proxies, the stamps |
| `nix/luks.nix`, `nix/luks/` | the initrd: unlock, in-place encryption, lima's key from cidata |
| `nix/egress.nix`, `nix/egress/yolobox-egress.nft` | the guest's egress table and its unit |
| `nix/hardening.nix` | blocked modules, the wrapper allowlist and agent groups with their one assertion, sysctls, `/proc` |
| `nix/tailscale.nix` | the optional tailnet |
| `nix/harnesses.nix`, `nix/harnesses/` | the claude launcher, its keeper, and the claude and pi installs |
| `nix/herd-server.nix`, `nix/pkgs/herdr-bin.nix` | the VM's herdr server and the pinned herdr release |
| `nix/display.nix`, `nix/display/` | Xvfb, openbox, Chromium and the screen recorder |
| `nix/podman.nix`, `nix/swap.nix`, `nix/lsp.nix` | rootless podman, the swapfile option, editor language servers through devbox |
| `nix/guest.nix`, `nix/guest/yolobox-guest.sh` | the guest helper; its header lists the subcommands |
| `nix/pkgs/yolobox.nix`, `homebrew/yolobox.rb` | `yo` packaged for nix and for brew |
| `lima/yolobox.yaml` | the Lima instance config, read once at creation |
| `libvirt/domain.xml` | the libvirt domain template; a stopped domain is redefined from it on every start |
| `linux/` | the Linux host's one-time setup script, its nft table and unit, and the passt ACL hook |
| `.github/workflows/` | `ci.yml` and `release.yml` both call `verify.yml` |
| `tests/` | the unit suite and the acceptance scripts |

`facts.json` reaches the shell scripts as `/etc/yolobox/facts.sh`, one `YOLOBOX_<KEY>` line per fact: a string or integer as itself, a list of strings as its words, a map of strings as `key:value` words in sorted key order; any other shape is refused at eval. A nix-built script also gets some facts through environment variables the build sets (`YOLOBOX_UNLOCK_*` for the initrd scripts, `YOLOBOX_XVFB_*` for the display), not only through `facts.sh`.

## Running the checks

```sh
python3 -m unittest discover -s tests        # CI runs 3.9 and the newest 3.x
uvx ruff@0.17.2 check . && uvx ruff@0.17.2 format --check .
shellcheck <the list in verify.yml's "Shellcheck every tracked shell script" step>
YOLOBOX_USERNAME=$(id -un) YOLOBOX_FLAKE_REF=github:aka-rider/yolobox/ci nix flake check --impure
```

- `ruff.toml` pins `required-version`, `verify.yml` installs the same version, and ruff refuses to run on any other. The guest's nixpkgs ruff is older and formats differently. Without uv, use a throwaway venv: `python3 -m venv /tmp/ruff && /tmp/ruff/bin/pip install ruff==0.17.2`. To bump it, change `ruff.toml`, `verify.yml` and this file together.
- The pinned shellcheck is shellcheck-py 0.11.0.1, as in `verify.yml`, and it installs on any host with pip: `python3 -m venv /tmp/sc && /tmp/sc/bin/pip install shellcheck-py==0.11.0.1`, then run `/tmp/sc/bin/shellcheck`.
- Python 3.9 is the floor because the Mac's Xcode Python is 3.9.
- Neither host is expected to have nix, so run nix inside a box. The copy must be operator-owned: `yo cp` lands files as the agent (uid 1000) and `yo ssh` is the operator (uid 501), so git reports `dubious ownership` and nix's libgit2 refuses the tree. Pipe an archive instead: `git -C <checkout> archive HEAD | yo ssh 'mkdir -p /tmp/yb && tar -x -C /tmp/yb'`. On a Mac set `COPYFILE_DISABLE=1` for any tar you make by hand, or macOS adds `._*` entries that ruff reports as E902.
- The guest operator has no `python3` on `PATH`; use `nix shell nixpkgs#python3 -c ...`.
- nix needs `YOLOBOX_USERNAME`, `YOLOBOX_FLAKE_REF` (a full `github:OWNER/REPO/REF`) and `--impure` on every command; `flake.nix` throws without them. `YOLOBOX_HOSTNAME` is optional, unset means `yolobox`, and a set value must be one lowercase DNS label. Inside a box, `yolobox-guest rebuild switch` sets all three from the stamps and the hostname.
- The guest's CPU is the evaluating nix's own: check an Apple silicon box from x86_64 with `nix eval --impure --system aarch64-linux ...`.
- `nix/base.nix` imports `/etc/yolobox/local.nix` when it exists, so a flake check inside a box evaluates that file too. To evaluate CI's box there, extend the configuration with `extendModules { modules = [ { disabledModules = [ "/etc/yolobox/local.nix" ]; } ]; }`.

## The test map

| Module | Covers | Skips without |
|---|---|---|
| `test_yo.py` | `yo`'s functions, argv builders, ssh config, `yo cp`, the broker client, the Lima driver | git, ssh, `os.pidfd_open` or `kqueue`; the download tests that run the real guest side skip without GNU tar and find |
| `test_yo_commands.py` | `main()` preflight, bootstrap, gc, flake-ref handling, status, up and unlock, down, disk-grow | |
| `test_yo_paths.py` | the project walk, the picker, version drift | |
| `test_yo_instance.py` | `-m/--machine` and several machines on Lima | ssh (one test) |
| `test_yo_libvirt.py` | the libvirt driver against `FakeHost`, a fake virsh | gawk, or nix to fetch it |
| `test_yo_luks.py` | the passphrase derivation, the ssh-agent protocol, the initrd verdicts | |
| `test_yo_guest_text.py` | guest output reaching the terminal escaped and capped | |
| `test_yo_cli_nobox.py` | no argparse `type=` callable reads the box | |
| `test_yo_aws.py` | `yo`'s side of the broker: the allow-list, the lock, the guest config | |
| `test_aws_broker.py` | the broker's handler over a real socket | `pidfd_open` or `kqueue` |
| `test_guest_script.py` | `yolobox-guest` subcommands under bash | GNU find, flock |
| `test_luks_scripts.py` | the three initrd scripts under bash | gawk |
| `test_screen_record.py` | the recorder under bash with a fake ffmpeg | Linux `/proc`, `pidfd_open`, flock |
| `test_xvfb.py` | the Xvfb wrapper under bash with a fake Xvfb and `systemd-notify` | |
| `test_harness_scripts.py` | the claude version predicate, launcher keeper and installer under bash | GNU find and sort |
| `test_box_checks.py` | `box-hardening-check.sh`: one green run per check family, then the refusals | GNU find, GNU stat |
| `test_host_setup.py` | `linux/host-setup.sh` rerun | GNU stat and install |
| `test_libvirt_hook.py` | the passt ACL hook | |
| `test_consistency.py` | static files that cannot read `facts.json` agree with it, plus repo-wide rules (the shellcheck list, the pinned ruff, the fail-closed units) | git, for the shellcheck-list test |

`tests/shell_harness.py` is the shared setUp of the bash modules: the script runs with `errexit nounset pipefail` as `writeShellApplication` runs it, fakes first on `PATH` (`ShellCase.fake`), `facts_sh()` renders `facts.json` as the box does, and `is_gnu(tool)` says whether a tool is GNU's, for skips.

`test_yo.py` holds `load_yo`, which loads the package as one namespace, `yo.NAME`. Patch the module whose global the code reads, `mock.patch.object(yo.proc, "run")`; the namespace refuses writes and refuses a name two modules define differently.

## Acceptance scripts on a throwaway box

`tests/box-hardening-check.sh` runs inside a box as the operator and proves the hardening holds and nothing the agent needs broke. `tests/box-hardening-e2e.sh NAME` drives it from the host across a reboot. `tests/box-libvirt-check.sh NAME` runs on a Linux host from the checkout and checks sVirt, the domain's devices and the host table; it deletes the guest's egress table for a while. None runs under `unittest`.

Never use the operator's working box. Make one:

```sh
yo -m e2e<N> up --memory 4 --cpus 2 --disk 40
yo -m e2e<N> bootstrap --version github:OWNER/REPO/<branch>
tests/box-hardening-e2e.sh e2e<N>
tests/box-libvirt-check.sh e2e<N>        # Linux host only
yo -m e2e<N> destroy --yes
```

- 4 GiB is the memory floor. The first bootstrap runs on the seed image, which has no swap yet: at 2 GiB the guest's OOM killer ended nix 166 s in, and at 4 GiB the build took 184 s with a 1.78 GB peak. 40 GiB is the disk floor.
- `yo up` on the fresh seed image succeeds, says the box is not bootstrapped yet and serves no forwards; `yo bootstrap` then builds it.
- On a Mac a named instance must exist before `yo up`; `yo` prints the `limactl create --set .ssh.localPort=<port>` it needs, with a port above 49151.
- A bootstrap builds only from a published ref, so push the branch first, and name it without a `/`.
- The throwaway box's memory counts against the host's room, beside the operator's box.
- The operator approves the 1Password prompt at each unlock. Say which command needs it and wait.

## Definition of done

A change is done when:

- the unit suite is green on Python 3.9 and the newest 3.x, and ruff check and format pass at the pinned version;
- shellcheck passes on `verify.yml`'s list, and a new script is on that list and, if the box builds it, in the "Build the shellchecked script packages" step;
- `nix flake check` passes in a box, for both guest systems when nix changed;
- a change under `nix/`, `linux/` or `libvirt/` passed the acceptance scripts on a throwaway box;
- `README.md`, `AGENTS.md` and `CONSTITUTION.md` say what is now true;
- every problem found on the way is fixed, not parked; `TODO.md` gains only an item that waits on an upstream change, with its link.

### Before a release

The release is a GitHub release with tag `vX.Y.Z` published from the tip of `main`; the workflow writes `.version` and moves the tag. The next one is `v2.0.0`: it removes `lanAllow`, the hardening escape hatches and the old stamps, and changes the guest contract, so every box is recreated. Run these live first; until each passed it is unproven:

- `nix flake check` and the box build for the x86_64 and aarch64 guests, and shellcheck over `verify.yml`'s list.
- `box-hardening-e2e.sh` and `box-libvirt-check.sh` on throwaway boxes, on a Mac and on a Linux host.
- On a Mac: `brew install --build-from-source` the formula and `brew test yolobox`, then `yo bootstrap` of a fresh box, its reboot into the encrypted generation, and `yo down && yo up`.
- On a Mac box, `echo $SSH_CONNECTION` in a `yo ssh` session starts with `192.168.5.2`. If it does not, `AllowUsers` locks both accounts out of every Mac box.
- `limactl start --timeout 4h` is accepted by the shipped Lima.
- On Lima, with no master alive, `yo ssh` issues both reverse forwards over `yo`'s own control master: `ssh-add -l` and `aws-creds` work as the agent.
- After boot, Xvfb and openbox are up with Xvfb as `Type=notify` and started with `-displayfd`, and Xvfb picked `:0`.
- The initrd's bash supports `wait -n -p` (bash 5.1 or newer), and a first boot encrypts and unlocks.
- `tailscale status --json` prints `"BackendState": "..."` in the shape `yolobox-guest status` parses.
- `yo cp :dir ~/x` on a Mac, whose `tar` is bsdtar: the download unpacks, and only regular files and directories land.
- One wrong passphrase: `crypttab` has `tries=1`, so the initrd may drop into emergency mode and the next `yo up` exits 5. Record what recovery takes.
- Four `yo -m e2e<N> down && yo -m e2e<N> up` cycles in a row each exit 0, and `yo status` then reports the 1Password forward up.
- On a Linux host, `yo up --memory N --cpus C --disk G` creating a box, and `yo disk-grow G`, against real virsh.
- In a box: `nixos-rebuild` of `github:O/R/<rev>?narHash=<hash>` is accepted with the hash's `+`, `/` and `=` unencoded and refused with one character changed; `yolobox-guest rebuild switch` after editing `local.nix` builds the stamped ref.
- In a herdr pane: `command -v claude` is `~/.local/bin/claude`, and after `claude update` the `yolobox-claude-launcher.path` unit is not `Trigger limit hit`.
- On a Linux host: whether a signature with `yolobox-operator` asked from an agent session prompts 1Password or rides the master's earlier approval.
- On a Mac, `yo cp` from the guest unpacks with bsdtar using `--no-same-owner --no-same-permissions --no-acls --no-xattrs --no-fflags --no-mac-metadata`; confirm bsdtar accepts every flag and a download lands.
- In the initrd, a wrong passphrase makes systemd-cryptsetup log `Passphrase incorrect?`, and `journalctl -n 1 --show-cursor` prints a cursor; the unlock helper's exit 1 versus exit 7 depends on both.
- A switch that changes the egress rules reloads yolobox-egress in place and does not restart dhcpcd or tailscaled.

## Facts the code does not tell you

Keys and the disk:

- The passphrase is an Ed25519 signature over a fixed message. RFC 8032 makes Ed25519 deterministic, and 1Password does not document that it follows it: confirm by deriving twice in one process after any 1Password upgrade.
- `cryptsetup --key-file -` takes the key byte for byte, a trailing newline included, so the scripts feed the passphrase without one.
- `cryptsetup reencrypt --device-size` keeps a sparse image small but leaves plaintext past the encrypted region. qemu's default `discard=ignore` makes a discard alone keep it, so the tail is zeroed through write-zeroes first, and only discarded where the disk has no write-zeroes offload.
- On Lima the initrd's sshd and stage 2's sshd answer on the same host port, so `yo` tells them apart by the banner word `yolobox-stage2` before every login.

SSH:

- A unix socket path must be shorter than 104 bytes on macOS and 108 on Linux, and OpenSSH binds a master at `<ControlPath>.<16 random characters>` first, so the ControlPath gets 17 bytes less.
- Lima rewrites its instance's `ssh.config` on every start, which is why `yo` writes its own file back after each start.
- sshd opens an `AuthorizedKeysFile` with the target user's privileges, so the agent cannot read the root-owned key file lima-init writes; it gets the key from a root-run `AuthorizedKeysCommand`.
- `ssh-add -l` saying "communication with agent failed" means a dead forward; "The agent has no identities" means a live one.

Network:

- In the output hook, `oifname` is the interface of the route chosen before NAT; a DNAT that reroutes does not update it. Hence the egress table judges destinations.
- passt keeps the guest's source port, so a fixed tailscaled port collides with the host's own tailscaled. Hence `port = 0`.
- In the Linux host table, `meta skuid` does not match a packet without a full socket (neighbour discovery, MLD, IGMP, kernel NFS, WireGuard), which is why the rule is a positive match on `yolobox-vm`.
- `dig` exits 0 on an ICMP unreachable, so a DNS probe must read the answer, not the exit code.

The host CLI and the broker:

- `yo ssh`, `enter`, `cp`, `code` and `zed` still run when the two reverse forwards cannot be set up, and print why. `yo ssh` is the way into a box whose forwards are broken.
- Only a guest session's output is quoted behind `  | `. A host tool's stderr (virsh, limactl, tar) is printed inline.
- A guest session's captured output is capped at 1 MiB per channel; past that the session is killed and named. The agent owns what a session prints.
- A `yo cp` download skips `node_modules` and `.venv` anywhere, and `target` and `.next` only beside a `Cargo.toml` or `package.json` (the facts `gc_build_dirs`, `gc_removed_outright` and `build_output_manifests`), and names each skip.
- A Lima instance that departs from `lima/yolobox.yaml` gets one remedy: a single `limactl edit` chain when every gap can be edited while stopped, otherwise a recreate that warns the disk is lost. Every recreate remedy `yo` prints is `yo destroy --yes && [limactl create ... &&] yo bootstrap`, built in one place.
- The broker serves each socket one connection at a time and queues the rest in a 64-deep listen backlog. A request line and its headers are capped at 8 KiB and must arrive within 10 s of the accept, however slowly they are sent. The reply is written under a 10 s timeout, and a credential mint is killed after 30 s.
- The broker's SIGTERM handler only sets an event; a thread cleans up and exits, because a handler that logs can deadlock on the log's lock.
- `yo` holds `aws-broker.yo.lock` across the whole of `ensure_aws_broker`, so two `yo` commands cannot race through read-allowlist, health, kill, start and push. The broker's own `<socket>.lock` is a different lock; holding it from `yo` makes every broker `yo` starts fail.

systemd and nix:

- systemd re-applies `RuntimeDirectory` ownership before every command it spawns, `+` ones included, so a `chgrp` in `ExecStartPre` is undone; it must be `ExecStartPost`.
- `PathModified` fires on every write; a ~330 MB claude download trips the path unit's `TriggerLimitBurst`. `PathChanged` fires a handful of times.
- `virtualisation.podman.autoPrune` renders a root timer that has nothing to prune on a rootless-only box, hence the agent's user timer. `du` undercounts rootless podman storage; use `podman system df`.
- A NixOS activation snippet must not `exit`: that skips the later snippets. Fail with `false`, which the runner's ERR trap records.
- The ESP holds one kernel set. When a switch behaves strangely, check `df /boot` and compare `readlink /run/current-system` with `readlink /nix/var/nix/profiles/system`; if they differ, do not garbage-collect.
