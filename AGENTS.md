# Working on yolobox

This file is for whoever changes the repo: where things live, how to check a change, when a change is done, and the facts the code cannot tell you. `CONSTITUTION.md` holds the rules; read it next. `README.md` is the operator's guide.

## The model in one screen

- **Two halves of one tool.** `yo` runs on the host and does everything the host does to the VM. `yolobox-guest` runs in the box and holds the guest-side logic `yo` calls over ssh. A new guest subcommand exists on a box only after the box is rebuilt.
- **Two accounts.** The operator has the host account's name, uid 501 and `wheel`. The agent is the constant account `agent`, uid 1000, with no `sudo`. Every coding session runs as the agent.
- **Two hosts, one guest.** `yo` picks a driver once, from the host OS: Lima (vz) on macOS, libvirt (`qemu:///system`, passt, sVirt) on Linux. The guest copies Lima's address plan on both (gateway 192.168.5.2, guest 192.168.5.15, NIC `enp0s1`) and is never told which OS runs it. A Mac runs its own CPU's guest; a Linux host runs x86_64 only.
- **Egress.** One constant nft table in the guest rejects traffic to the host and to private networks, except DNS to the gateway and DHCP. `tailscale0` is trusted; the tailnet ACL is the gate there. `dhcpcd` and `tailscaled` require the table's unit, so a box whose table fails to load at boot has no address and is unreachable; recreate it. A switch reloads the table in place and never restarts either. `yolobox-network-ready` fails unless the NIC holds an IPv4 address now (`ip -4 -o addr show`; dhcpcd's lease file outlives a boot, so a lease alone can be last boot's) and `dhcpcd --dumplease` shows the lease brought a `domain_name_servers` line with a value, printing the lease on stderr, and `yo status` warns while that unit is not active. A Linux host adds its own table, keyed on the uid `yolobox-vm`, that the guest cannot undo.
- **Git is the bridge.** No host mounts. Checkouts in the VM accept pushes with `receive.denyCurrentBranch = updateInstead`.
- **Encrypted root.** `/dev/vda2` is LUKS2. `yo up` derives the passphrase from the 1Password key `yolobox-disk` only for an sshd whose banner carries the initrd's word `yolobox-initrd`, read under the box lock after proving the listener is the VM's (`unlock.initrd_endpoint()`, the one gate of every `root` login), and feeds it to that sshd. Any other sshd, the seed image's unmarked one included, is asked for the operator alone, so a plaintext box never prompts. Before it starts a box that exists and is off, which may be encrypted, `yo up` lists 1Password's agent keys (a listing, no signature, so no prompt) and refuses while the box is still off unless `yolobox-disk` is there. The seed image's first boot of a yolobox generation encrypts the plain root in place.
- **Published flake ref.** `yo bootstrap` builds `github:aka-rider/yolobox/<rev>?narHash=<hash>` resolved from a release, branch or `github:OWNER/REPO/REF`. The box stamps that ref into `/etc/yolobox/flake-ref`, and `yolobox-guest rebuild` builds it again and refuses a stamp that does not start with `github:`. The ref names the full rev, so every new rev is a new system generation, a docs-only commit included. The VM holds no checkout of this repo.
- **Secrets over reverse unix sockets.** 1Password's agent and the AWS broker are forwarded by `yo`'s own operator ssh master, once the box is bootstrapped, into `/run/yolobox-op/` (operator-owned) and proxied by `systemd-socket-proxyd` to agent-owned sockets in `/run/yolobox/`. Nothing sensitive is copied in. The proxy sockets are wanted by no target: a socket that accepts before its forward exists lets a connection from a boot-time agent process fail in the proxy and break the client for good. `forwards.ensure_session` starts both (one `sudo systemctl start` as the operator) right after it asks the master for the forwards, and probes nothing. `vm_services` declares everything first (the session, then the AWS broker), so a down forward never keeps another service from coming up; it then probes the 1Password forward, and the AWS forward only when the guest has AWS, and raises one error naming every forward that is down with its probe's words and `yo up`. `yo port` needs only the master, so it calls `forwards.open_master`, not the session. A box rebooted since its last `yo` command shows the sockets `inactive`, and `yo status` says so and names `yo up`.

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
| `linux/` | the Linux host's one-time setup script, its nft table, its unit (which also applies the passt directory's tmpfiles rule each boot) and the passt ACL hook |
| `.github/workflows/` | `ci.yml` and `release.yml` both call `verify.yml` |
| `tests/` | the unit suite and the acceptance scripts |

`facts.json` reaches the shell scripts as `/etc/yolobox/facts.sh`, one `YOLOBOX_<KEY>` line per fact: a string or integer as itself, a list of strings as its words, a map of strings as `key:value` words in sorted key order; any other shape is refused at eval. A nix-built script also gets some facts through environment variables the build sets (`YOLOBOX_UNLOCK_*` for the initrd scripts, `YOLOBOX_XVFB_*` for the display), not only through `facts.sh`.

## Running the checks

```sh
python3 -m unittest discover -s tests        # CI runs 3.9 and the newest 3.x
uvx ruff@0.17.2 check . && uvx ruff@0.17.2 format --check .
shellcheck <the list in verify.yml's "Shellcheck every tracked shell script" step>
YOLOBOX_USERNAME=$(id -un) YOLOBOX_FLAKE_REF=github:aka-rider/yolobox/ci nix eval --impure --eval-system x86_64-linux --raw .#nixosConfigurations.yolobox.config.system.build.toplevel.drvPath   # and aarch64-linux
```

- There is no `nix flake check`. The flake has no `checks` output, so it would only evaluate the box's toplevel for one guest system and `yo`'s package as a derivation, both less than CI's eval job does: it evaluates the toplevel for both guest systems, checks its settings on each, and builds `yo` and the shellchecked scripts.

- `ruff.toml` pins `required-version`, `verify.yml` installs the same version, and ruff refuses to run on any other. The guest's nixpkgs ruff is older and formats differently. Without uv, use a throwaway venv: `python3 -m venv /tmp/ruff && /tmp/ruff/bin/pip install ruff==0.17.2`. To bump it, change `ruff.toml`, `verify.yml` and this file together.
- The pinned shellcheck is shellcheck-py 0.11.0.1, as in `verify.yml`, and it installs on any host with pip: `python3 -m venv /tmp/sc && /tmp/sc/bin/pip install shellcheck-py==0.11.0.1`, then run `/tmp/sc/bin/shellcheck`.
- Python 3.9 is the floor because the Mac's Xcode Python is 3.9.
- CI runs the suite with `YOLOBOX_FORBID_SKIP=1`, where a test that skips although its tool is on `PATH` fails. Leave it unset on a Mac: its BSD `find` and `stat` are on `PATH` but are not the GNU tools those tests need.
- Neither host is expected to have nix, so run nix inside a box. The copy must be operator-owned: `yo cp` lands files as the agent (uid 1000) and `yo ssh` is the operator (uid 501), so git reports `dubious ownership` and nix's libgit2 refuses the tree. Pipe an archive instead: `git -C <checkout> archive HEAD | yo ssh 'mkdir -p /tmp/yb && tar -x -C /tmp/yb'`. On a Mac set `COPYFILE_DISABLE=1` for any tar you make by hand, or macOS adds `._*` entries that ruff reports as E902.
- The guest operator has no `python3` on `PATH`; use `nix shell nixpkgs#python3 -c ...`.
- nix needs `YOLOBOX_USERNAME`, `YOLOBOX_FLAKE_REF` (a full `github:OWNER/REPO/REF`) and `--impure` on every command; `flake.nix` throws without them. `YOLOBOX_HOSTNAME` is optional, unset means `yolobox`, and a set value must be one lowercase DNS label. Inside a box, `yolobox-guest rebuild switch` sets all three from the stamps and the hostname.
- The guest's CPU is the evaluating nix's own: evaluate the other guest system with `--eval-system`, as `nix eval --impure --eval-system x86_64-linux ...` on an Apple silicon box and `--eval-system aarch64-linux` on an x86_64 one. Never `--system`: it is a restricted setting the nix daemon may ignore for an untrusted user, and nix then evaluates the native system without a word. `nix derivation show <the .drv>` prints the `"system"` that was evaluated, which is the proof.
- `nix/base.nix` imports `/etc/yolobox/local.nix` when it exists, so an evaluation inside a box evaluates that file too. To check CI's box there, add `YOLOBOX_SKIP_LOCAL_NIX=1` (the fact `nix_env_skip_local_nix`) to the command, which leaves the import out.

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
| `test_luks_scripts.py` | the three initrd scripts under bash | gawk; the unlock helper's tests need bash 5.1 |
| `test_screen_record.py` | the recorder under bash with a fake ffmpeg | Linux `/proc`, `pidfd_open`, flock |
| `test_xvfb.py` | the Xvfb wrapper under bash with a fake Xvfb and `systemd-notify` | |
| `test_harness_scripts.py` | the claude version predicate, launcher keeper and installer, and the network-ready flag script, under bash | GNU find and sort |
| `test_box_checks.py` | `box-hardening-check.sh`: one green run per check family, then the refusals | GNU find, GNU stat |
| `test_host_setup.py` | `linux/host-setup.sh` rerun | GNU stat and install |
| `test_libvirt_hook.py` | the passt ACL hook | |
| `test_consistency.py` | static files that cannot read `facts.json` agree with it, plus repo-wide rules: the shellcheck list, the pinned ruff, the fail-closed units, the banner marks spelled only through their facts, no file written outside `proc.atomic_file` (counted per function), no `.search`/`.match` but the allow-listed value extractions, no `mktemp` nested or after `local`, no pipe into `grep -q`, every `sort` under `LC_ALL=C` (inline, or after a top-level `export`), every refusal in `box-hardening-check.sh` after a `positive_control` of the same probe, no skip for a tool but through the harness, no ssh command line outside `sshx` (and the two named exceptions), the unpinned root login built only from a proved initrd, and the harness's own guards: no test reaching the real home, from a thread too, and no test that prints | git and a checkout, for the tests that list tracked files |

`tests/shell_harness.py` is imported by every test module (`test_yo.py` imports it too). At import it points `$HOME` at a scratch directory under `/tmp`, unsets `AWS_CONFIG_FILE` (a box's environment names the agent's real AWS config), and installs an audit hook that fails a test with `RealHomeTouched` when the test's own Python, in any of its threads, opens, lists, creates or connects to a path under the operator's real home; only the checkout and the running Python's own files are read there. A subprocess is outside the hook: bash under `ShellCase` gets the scratch `$HOME` in its environment, and ssh, which takes `~` from the account database whatever `$HOME` says, meets a guard first on `PATH` that exits 97 unless it is given `-F`, so no ssh a test runs reads the real `~/.ssh`. A test may not print either: the harness takes each test's stdout and stderr, the descriptors its child processes inherit included, and fails a test that wrote anything, so a clean run shows only unittest's own output; a test that expects output captures it and checks it. It is also the shared setUp of the bash modules: the script runs with `errexit nounset pipefail` as `writeShellApplication` runs it, fakes first on `PATH` (`ShellCase.fake`), `facts_sh()` renders `facts.json` as the box does, and `is_gnu(tool)` and `bash_version()` judge a tool, for skips. Every skip for a missing tool goes through `needs_tool` (a decorator) or `require_tool` (inside a test); with `YOLOBOX_FORBID_SKIP=1`, which CI's unit job sets, a skip whose tools are all on `PATH` fails instead. A skip for a Python feature (`os.pidfd_open`, `kqueue`) or for running as root is not a tool's and stays a plain skip.

`test_yo.py` holds `load_yo`, which loads the package as one namespace, `yo.NAME`, running as the host account `HOST_ACCOUNT` (`op`) whoever runs the suite: `yo` refuses an operator named `agent`, and the suite also runs inside a box as `agent`. Patch the module whose global the code reads, `mock.patch.object(yo.proc, "run")`; the namespace refuses writes and refuses a name two modules define differently. A step that takes a `HeldBox` is called under `yo.box_lock(wait=True)` (or `locked()`); a test that needs another `yo` to hold the lock uses `box_lock_held_elsewhere()`, and `box_lock_is_held()` probes it through a descriptor of its own, as another process would.

## Acceptance scripts on a throwaway box

`tests/box-hardening-check.sh` runs inside a box as the operator and proves the hardening holds and nothing the agent needs broke. `tests/box-hardening-e2e.sh NAME` drives it from the host across a reboot. `tests/box-libvirt-check.sh NAME` runs on a Linux host from the checkout and checks sVirt, the domain's devices, the guest's resolver and the host table; it deletes the guest's egress table for a while. None runs under `unittest`.

Never use the operator's working box. Make one:

```sh
yo -m e2e<N> up --memory 4 --cpus 2 --disk 40
yo -m e2e<N> bootstrap --version github:OWNER/REPO/<branch>
tests/box-hardening-e2e.sh e2e<N>
tests/box-libvirt-check.sh e2e<N>        # Linux host only
yo -m e2e<N> destroy --yes
```

- 4 GiB is the memory floor. The first bootstrap runs on the seed image, which has no swap yet: at 2 GiB the guest's OOM killer ended nix 166 s in, and at 4 GiB the build took 184 s with a 1.78 GB peak. 40 GiB is the disk floor.
- `yo up` on a fresh box asks 1Password nothing (the seed image's root is plaintext), succeeds, says in one line the box is not bootstrapped yet and serves no forwards; `yo bootstrap` then builds it. "Bootstrapped" is the box's own declaration: `/etc/yolobox/version` (the fact `version_stamp`) exists.
- On a Mac `yo -m NAME up` creates a named instance itself, on the lowest free ssh port above 49151 that no other instance claims, picked and taken under the host's port lock.
- A bootstrap builds only from a published ref, so push the branch first, and name it without a `/`.
- The throwaway box's memory counts against the host's room, beside the operator's box.
- The operator approves the 1Password prompt at each unlock. Say which command needs it and wait.

## Definition of done

A change is done when:

- the unit suite is green on Python 3.9 and the newest 3.x, and ruff check and format pass at the pinned version;
- shellcheck passes on `verify.yml`'s list, and a new script is on that list and, if the box builds it, in the "Build the shellchecked script packages" step;
- the box's toplevel evaluates in a box for both guest systems when nix changed, and CI's eval job passes;
- a change under `nix/`, `linux/` or `libvirt/` passed the acceptance scripts on a throwaway box;
- `README.md`, `AGENTS.md` and `CONSTITUTION.md` say what is now true;
- every problem found on the way is fixed, not parked; `TODO.md` gains only an item that waits on an upstream change, with its link.

### Before a release

The release is a GitHub release with tag `vX.Y.Z` published from the tip of `main`; the workflow writes `.version` and moves the tag. The next one is `v2.0.0`: it removes `lanAllow`, the hardening escape hatches and the old stamps, and changes the guest contract, so every box is recreated. Run these live first; until each passed it is unproven:

- The box's evaluation and build for the x86_64 and aarch64 guests, and shellcheck over `verify.yml`'s list.
- `box-hardening-e2e.sh` and `box-libvirt-check.sh` on throwaway boxes, on a Mac and on a Linux host.
- On a Mac: `brew install --build-from-source` the formula and `brew test yolobox`, then `yo bootstrap` of a fresh box, its reboot into the encrypted generation, and `yo down && yo up`.
- On a Mac box, `echo $SSH_CONNECTION` in a `yo ssh` session starts with `192.168.5.2`. If it does not, `AllowUsers` locks both accounts out of every Mac box.
- `limactl start --timeout 4h` is accepted by the shipped Lima.
- On Lima, `yo ssh` issues both reverse forwards over `yo`'s own control master (`yo-op.sock`, never Lima's `ssh.sock`): `ssh-add -l` and `aws-creds` work as the agent on a box created from the current `lima/yolobox.yaml`.
- After boot, Xvfb and openbox are up with Xvfb as `Type=notify` and started with `-displayfd`, and Xvfb picked `:0`.
- The initrd's bash supports `wait -n -p` (bash 5.1 or newer), and a first boot encrypts and unlocks.
- `tailscale status --json` prints `"BackendState": "..."` in the shape `yolobox-guest status` parses.
- `yo cp :dir ~/x` on a Mac, whose `tar` is bsdtar: the download unpacks, and only regular files and directories land.
- A `yo cp` round trip of a tree with non-ASCII names (`café.txt`, `日本語/ファイル.txt`) up and down lands every name byte for byte, on a Mac and on a Linux host. bsdtar 3.5.3 under `LC_ALL=C` keeps a GNU-format member's raw bytes and NFD-normalises a PAX one in every locale, so also check that the guest's `tar --show-defaults` says `--format=gnu`.
- As the agent, `/run/wrappers/bin/newuidmap` with no arguments prints its usage and exits 1: `check_sudo_refuses_agent`'s positive control waits for that exit.
- One wrong passphrase: `crypttab` has `tries=1`, so the initrd may drop into emergency mode and the next `yo up` exits 5. Record what recovery takes.
- Four `yo -m e2e<N> down && yo -m e2e<N> up` cycles in a row each exit 0, and `yo status` then reports the 1Password forward up.
- On a Linux host, `yo up --memory N --cpus C --disk G` creating a box, and `yo disk-grow G`, against real virsh.
- In a box: `nixos-rebuild` of `github:O/R/<rev>?narHash=<hash>` is accepted with the hash's `+`, `/` and `=` unencoded and refused with one character changed; `yolobox-guest rebuild switch` after editing `local.nix` builds the stamped ref.
- In a herdr pane: `command -v claude` is `~/.local/bin/claude`, and after `claude update` the `yolobox-claude-launcher.path` unit is not `Trigger limit hit`.
- On a Linux host: whether a signature with `yolobox-operator` asked from an agent session prompts 1Password or rides the master's earlier approval.
- On a Mac, `yo cp` from the guest unpacks with bsdtar using `--no-same-owner --no-same-permissions --no-acls --no-xattrs --no-fflags --no-mac-metadata`; confirm bsdtar accepts every flag and a download lands.
- In the initrd, a wrong passphrase makes systemd-cryptsetup log `Passphrase incorrect?`, and `journalctl -n 1 --show-cursor` prints a cursor; the unlock helper's exit 1 versus exit 7 depends on both.
- A switch that changes the egress rules reloads yolobox-egress in place and does not restart dhcpcd or tailscaled.
- `dhcpcd --dumplease enp0s1` as root on a booted box prints `domain_name_servers=` followed by the lease's servers (the script's test fakes that shape; it was written from dhcpcd's documentation, not a live box), and `yolobox-network-ready` is active.
- After a reboot and before any `yo` command, `systemctl is-active yolobox-op-agent.socket yolobox-aws-broker.socket` says `inactive`, and herdr connecting in that window cannot reach a half-made proxy; the next `yo ssh` makes both `active` and `ssh-add -l` works as the agent.
- In a box with a `local.nix`, `YOLOBOX_SKIP_LOCAL_NIX=1` on the `nix eval` leaves the file out of the evaluation.
- Kill the operator's master (`ssh -O exit` on `yo-op.sock`, or a guest reboot): `yo status` reports the 1Password forward down and, when AWS is enabled (the `aws` CLI is installed and `~/.aws/config` has SSO profiles), the AWS forward too, and names `yo up`, and the next `yo ssh true` restores them (and, on Linux, the broker): `ssh-add -l` and `aws-creds` work as the agent again.
- A box whose initrd asks: `yo up` prompts once; a fresh box that was stopped before its bootstrap: `yo up` prompts never.
- The initrd's sshd sends `yolobox-initrd` in its banner (`ssh-keyscan -p <port> 127.0.0.1` while the box waits for its passphrase), and on libvirt `yo ssh true` against a box already up makes no 1Password prompt beyond the masters' first logins.
- A second `ssh -O forward -R` of a forward the master already carries exits 0, and with `StreamLocalBindUnlink yes` a forward binds over a socket a dead master left in `/run/yolobox-op`: `yo ssh` twice, then kill the master and `yo ssh` again, and `ssh-add -l` works as the agent each time.
- On a Mac, `yo -m e2e<N> up --memory 4 --cpus 2 --disk 40` creates the instance with `limactl create --set .ssh.localPort=N`, and Lima's hostagent listens on N.
- On a Mac, while a box waits in its initrd for the passphrase, `lsof -nP -iTCP:<port> -sTCP:LISTEN -Fp` names the hostagent's pid alone, as it does for a box in stage 2.

## Facts the code does not tell you

Keys and the disk:

- The passphrase is an Ed25519 signature over a fixed message. RFC 8032 makes Ed25519 deterministic, and 1Password does not document that it follows it: confirm by deriving twice in one process after any 1Password upgrade.
- The initrd's unlock helper ends a finished unlock with the line `yolobox-unlock: unlocked` (the fact `unlock_unlocked_line`); `yo` takes exit 0 without that last line for an error, not an unlock. Its failures are the `unlock_exit_*` codes, and its ERR trap turns any other failure into `unlock_exit_unexpected`.
- On libvirt `<instance dir>/disk-volume` records the disk volume's build: `building` before the volume is created, `built` after its resize. `yo` rebuilds only a volume recorded `building`, and refuses to touch one it has no record of.
- `cryptsetup --key-file -` takes the key byte for byte, a trailing newline included, so the scripts feed the passphrase without one.
- `cryptsetup reencrypt --device-size` keeps a sparse image small but leaves plaintext past the encrypted region. qemu's default `discard=ignore` makes a discard alone keep it, so the tail is zeroed through write-zeroes first, and only discarded where the disk has no write-zeroes offload.
- On Lima the initrd's sshd and stage 2's sshd answer on the same host port, so `yo` tells them apart by their banner words before every login: `yolobox-initrd` (the fact `initrd_ssh_banner_mark`, `VersionAddendum` in `nix/luks.nix`) and `yolobox-stage2` (`stage2_ssh_banner_mark`, in `nix/base.nix`). An ssh banner with neither word is the seed image's stage 2; a line that is no ssh identification is something else on the port, and an error that quotes it. The banner is read again right before the passphrase is derived and sent.

SSH:

- A unix socket path must be shorter than 104 bytes on macOS and 108 on Linux, and OpenSSH binds a master at `<ControlPath>.<16 random characters>` first, so the ControlPath gets 17 bytes less.
- Lima rewrites its instance's `ssh.config` on every start, which is why `yo` writes its own file back after each start.
- Lima's hostagent keeps its own persistent master at the instance's `ssh.sock`, so `yo`'s operator master lives at `yo-op.sock`. `yo` asks that master for both reverse forwards whenever it declares the services, since any session (git, an editor, `yo gc`) may have opened it without them.
- ssh asks for agent forwarding only while it holds an identity agent. The Lima roles' config says `IdentityAgent none`, so `yo ssh` passes `IdentityAgent` and `ForwardAgent` with the same 1Password socket; `IdentitiesOnly yes` keeps its login on Lima's key, so 1Password signs nothing for it.
- Lima 2 pins the guest's host key in the instance's `ssh.known_hosts` and logs `host key mismatch` when the initrd's per-boot key answers instead. The warning is Lima's; the initrd's key is never stored, so it cannot be made to match. `yo` checks its own pin and the banner. `yo` starts Lima with `--log-level=warn`, so only Lima's warnings and errors reach the terminal.
- `yo`'s own pin is the instance's `known_hosts`, with its lines under the `HostKeyAlias` that both ssh blocks name (`vm_host`), not under `[127.0.0.1]:port`: the ssh port can change (`limactl edit --set .ssh.localPort`), and a pin keyed on it would be missed, and the new port's answer learned by `accept-new`. The fetch that writes it proves the listener the VM's first (`sshx.proved_ssh_facts()`, the same proof as the initrd's gate), dials the port it proved with an explicit `Port`, not the one `ssh.config` names, and fails on any line but a blank one or an `ssh-rsa` or `ssh-ed25519` host key, the two NixOS generates. First contact checks no host key, so without that proof a process already on the port would be offered the operator's key and could have its own key pinned.
- sshd opens an `AuthorizedKeysFile` with the target user's privileges, so the agent cannot read the root-owned key file lima-init writes; it gets the key from a root-run `AuthorizedKeysCommand`.
- `ssh-add -l` saying "communication with agent failed" means a dead forward; "The agent has no identities" means a live one.
- ssh exits 255 both when it cannot connect and, without a word, when the remote command dies of a signal; only a 255 with ssh's own message means the box is out of reach.
- `ssh -E FILE` writes ssh's own messages to FILE and leaves the remote command's stderr on stderr. sshd refusing every key makes ssh print `USER@HOST: Permission denied (publickey).` there; the agent's rc files can print the same line on the session's stderr and exit 255, so `yo` judges the agent's login (`sshx.broken_agent_login`, which runs `echo yolobox-agent-login`) by that file alone.
- On a Mac, Lima's hostagent itself listens on the box's ssh port. `lsof -nP -iTCP:PORT -sTCP:LISTEN -Fp` prints a `p<pid>` line per process holding a listener and an `f<fd>` line per descriptor, and exits 1 with no output when nothing listens (and for other failures too). `yo` proves the only listener is the hostagent (`limactl list`'s `hostAgentPID`, which Lima reads from `ha.pid`) under the box lock at three points: at the initrd gate and before the host-key fetch, both through `sshx.proved_ssh_facts()`, and again right before every `root` login (`unlock.root_argv()`). On Linux `ss` proves it passt's at the same three points, and there no listener at the gate is fine, since the check before root's login sees who took the port since.
- `ssh -O check` prints `Master running (pid=N)` for a live master, and `Control socket connect(PATH): ...` when nothing answers the socket; `yo` reads a master only from those words.

Network:

- In the output hook, `oifname` is the interface of the route chosen before NAT; a DNAT that reroutes does not update it. Hence the egress table judges destinations.
- passt keeps the guest's source port, so a fixed tailscaled port collides with the host's own tailscaled. Hence `port = 0`.
- passt reads the host's resolver at start and puts it into the guest's DHCP lease. `passt_t` may read only `net_conf_t`, and a resolver file (resolved's stub, which `/etc/resolv.conf` links to) that a root editor replaced by rename keeps the editor's label, so passt is denied, the lease has no DNS server and the guest boots without DNS, silently. Hence a start (never ssh, status, down or destroy) and `host-setup.sh` refuse a resolver whose label's type field is not `net_conf_t`, read with `getxattr` and `stat -c %C`. With SELinux disabled the kernel returns no `security.selinux` attribute (ENODATA or EOPNOTSUPP) and `stat` prints `?`; no label can deny passt then, so that passes. A local policy module that lets passt read the wrong label is the wrong fix: chronyd is denied the same read.
- The passt directory's tmpfiles rule lives in `/etc/yolobox/yolobox-passt.conf` and `yolobox-host.service` applies it before virtqemud on every boot. Never put it in `/etc/tmpfiles.d`: dracut copies that directory into the initrd, which has no `qemu` account, and every boot then logs `Failed to resolve user 'qemu'`. The libvirt hook cannot create the directory itself, since it returns when the directory does not exist yet.
- `ss -e` prints `ino:` for every socket and `uid:` only for an owner that is not root (iproute2's `sock_details`), so a listener line with its `ino:` and no `uid:` is root's.
- In the Linux host table, `meta skuid` does not match a packet without a full socket (neighbour discovery, MLD, IGMP, kernel NFS, WireGuard), which is why the rule is a positive match on `yolobox-vm`.
- `dig` exits 0 on an ICMP unreachable, so a DNS probe must read the answer, not the exit code.
- gvproxy, Lima's gateway, answers some DNS questions itself (`. NS`), while a name it must forward to the host can time out; hence the gateway probe resolves a real name.
- The guest resolves through the gateway, so `yolobox-guest status` probes egress at an address (`https://1.1.1.1`): a probe by name would read a dead gateway DNS as dead egress.
- `yolobox-guest status` prints `internet=` as `ok` (curl got an HTTP status), `dead` (curl's exit 7, could not connect, or 28, timed out) or `unknown: ` and curl's words, and `gateway_dns=` as `ok` (an address came back), `dead` (no address, or dig's own no-reply line) or `unknown: ` and dig's words. A no-reply line starts `;;` and says `timed out`, `no servers could be reached`, `connection refused` or `unreachable`, in any case: bind 9.18 and later print `;; communications error to 192.168.5.2#53: connection refused` and `host unreachable`, the egress table's ICMP answer.

The host CLI and the broker:

- `yo ssh`, `enter`, `cp`, `code` and `zed` first declare a bootstrapped box's services as `yo up` does (the operator's master, both forwards, the AWS broker), so any of them restores a master, forward or broker that died; on a box not bootstrapped yet they declare nothing and say nothing. They still run when the services cannot be set up, and print why. `yo ssh` is the way into a box whose forwards are broken. `yo status` names `yo up` beside a forward that is down; the same commands print a forward that is down right after `yo` declared it as a refusal, and carry on.
- Only a guest session's output is quoted behind `  | `. A host tool's stderr (virsh, limactl, tar) is printed inline.
- A guest session's captured output is capped at 1 MiB per channel; past that the session is killed and named. The agent owns what a session prints.
- A `yo cp` download skips `node_modules` and `.venv` anywhere, and `target` and `.next` only beside a `Cargo.toml` or `package.json` (the facts `gc_build_dirs`, `gc_removed_outright` and `build_output_manifests`), and names each skip.
- A Lima instance that departs from `lima/yolobox.yaml` gets one remedy: a single `limactl edit` chain when every gap can be edited while stopped, otherwise a recreate that warns the disk is lost. Every recreate remedy `yo` prints is `yo destroy --yes && yo bootstrap`, built in one place that always prefixes it with the warning that recreating the box deletes everything on it, so no caller can print it without.
- The broker serves each socket one connection at a time and queues the rest in a 64-deep listen backlog. A request line and its headers are capped at 8 KiB and must arrive within 10 s of the accept, however slowly they are sent. The reply is written under a 10 s timeout, and a credential mint is killed after 30 s.
- `yo` asks the broker's control socket for `/health`: a JSON object is a broker, no socket or a refused connect is none, and any other answer is an error that shows it, since that broker still holds the socket.
- The broker's SIGTERM handler only sets an event; a thread cleans up and exits, because a handler that logs can deadlock on the log's lock.
- Each box has its own broker in `<instance dir>/aws-broker/`: `guest.sock` (forwarded into that box), `ctl.sock` (yo's `/health`), `yo.lock` and `broker.log`. The instance directory is `~/.lima/<name>` on a Mac and `~/.local/state/yolobox/instances/<name>` on Linux. `-m` is refused while the command line is parsed when `$HOME` plus the machine name leaves any of the box's sockets too long, the default machine included (`state.sockets_refusal`). `yo destroy --yes` stops the box's broker before the driver's pieces go: on Linux the broker watches the operator's master, and one that ends with it unlinks its sockets while the directory is deleted. `yo` holds `yo.lock` across the whole of `ensure_aws_broker`, so two `yo` commands cannot race through read-allowlist, health, kill, start and push. The broker's own `<socket>.lock` is a different lock; holding it from `yo` makes every broker `yo` starts fail.
- `<box>.box.lock` in the state directory is the box's own lock. `yo up`, `yo bootstrap` and `yo disk-grow` take it once, before their first read of the box, wait for it (saying in one line that another `yo` holds it, and which commands may), and hold it to their end, the rebuild, the resize, the herdr machine and the services included; `yo down` and `yo destroy --yes` hold it for their whole run and refuse while another `yo` holds it; `yo status` takes it without waiting only to look inside an initrd as `root`, and says so when another `yo` holds it. `ssh-config.lock` guards the include line in the shared `~/.ssh/config`, which is read again under it.
- Every tool `yo` runs through `proc` runs under `LC_ALL=C`, and `sshx.guest_argv`, which `ssh_run` and `yo cp`'s download build on, starts every guest command with `env LC_ALL=C`, since `yo`'s ssh config passes no `SendEnv` and the guest would otherwise use its own locale; no ssh command line is built outside `sshx` but the initrd's unpinned login and `ssh -G`. `yolobox-guest` and every script that matches a tool's words export `LC_ALL=C` at their top. Only the interactive `exec` of `yo ssh` and `fzf` keep the operator's locale.

Hosts and shells:

- `sudo -u agent CMD` runs in the caller's cwd, and the operator's home is 0700, so give it `env -C /`.
- On a Mac `/tmp`, `/var` and `/etc` are symlinks into `/private`, and `yo cp` refuses an upload spelled through them; spell the path as `pwd -P` prints it.
- bash's `cd ""` succeeds and stays where it is.
- On a Mac `$TMPDIR` is a long `/var/folders/...` path (a ControlPath under it passes 104 bytes), `PATH_MAX` is 1024 and `/bin/bash` is 3.2, so the tests that need bash 5.1 skip through `bash_version()`. Xcode's Python caches `.pyc` files in `~/Library/Caches/com.apple.python` keyed by mtime and size at 1 s resolution: run the suite with `PYTHONDONTWRITEBYTECODE=1` while flipping a test, and clear that cache and `__pycache__` after, or an edit made within the same second runs the old code.
- nix's `builtins.match` is POSIX ERE over the whole string (`\s` is a literal `s`), and nix sorts strings bytewise, so compare its lists with `LC_ALL=C sort`.

systemd and nix:

- systemd re-applies `RuntimeDirectory` ownership before every command it spawns, `+` ones included, so a `chgrp` in `ExecStartPre` is undone; it must be `ExecStartPost`.
- `PathModified` fires on every write; a ~330 MB claude download trips the path unit's `TriggerLimitBurst`. `PathChanged` fires a handful of times.
- `virtualisation.podman.autoPrune` renders a root timer that has nothing to prune on a rootless-only box, hence the agent's user timer. `du` undercounts rootless podman storage; use `podman system df`.
- A NixOS activation snippet must not `exit`: that skips the later snippets. Fail with `false`, which the runner's ERR trap records.
- The ESP holds one kernel set. When a switch behaves strangely, check `df /boot` and compare `readlink /run/current-system` with `readlink /nix/var/nix/profiles/system`; if they differ, do not garbage-collect.
