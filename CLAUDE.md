# yolobox: a tour for whoever works on this repo next

Welcome. This file is a short guided tour: what the box is, how its parts fit
together, where each part lives, and how to build and test a change. It does
not carry the long reasoning. That sits in story files beside the code, and
`docs/README.md` indexes them.

Read `CONSTITUTION.md` before your first change. It is the list of rules this
repo follows, and each rule links to the story that earned it.

## What the box is

yolobox is one NixOS VM on a Mac, run by Lima, where coding agents work with
no host mounts. Git is the only bridge between the Mac and the VM. The
README gets a person from nothing to a working project; this file is for
people who change the repo itself. Version 1 was Docker-based and lives at
commit `1fed88d`.

## The mental model

- **Two halves of one tool.** `yo` runs on the Mac and does everything the
  Mac does to the VM. `yolobox-guest` runs inside the box and holds the
  guest-side logic `yo` calls over ssh. `yo` reads exit codes to learn what
  the box supports, so a new guest subcommand needs a box rebuild before it
  exists. See `yo.md` and `nix/guest/yolobox-guest.md`.
- **Two accounts.** The operator has the Mac account's name and uid 501, and
  is the only account in `wheel`. The agent is a constant account, uid 1000,
  with no `sudo`. Every AI coding session runs as the agent, so it cannot
  reach root. Lima would re-grant `wheel` to its own account on every boot,
  which is why the agent is a separate account. See `docs/accounts.md`.
- **Egress is open unless the box opts in to a kill switch.** By default the box reaches the
  internet through the Lima NAT. A box that names `yolobox.network.exitNode`
  in `/etc/yolobox/local.nix` arms a kill switch instead: an nft table (loaded
  by the generation, with no stop hook and no off switch) rejects every packet
  that does not leave through that tailnet exit node, and DNS is plain DNS to a
  resolver on the tailnet. That box must also name `networking.nameservers`,
  and its tailnet ACL must grant it `autogroup:internet`. These are
  prerequisites of opting in, not of yolobox. With no exit node named the
  kill switch is absent. See `nix/killswitch.md`.
- **Git is the bridge.** `yo link` makes the VM side of a repo a
  push-to-checkout target (`receive.denyCurrentBranch = updateInstead`), so
  the VM refuses a push when its worktree differs from HEAD. The VM commits
  `devbox.lock`, so pull before you push again.
- **The root disk is encrypted.** `/dev/vda2` is LUKS2; `yo up` derives the
  passphrase from a 1Password SSH key and feeds it to an sshd inside the
  initrd. See `docs/disk-encryption.md`.
- **The box builds from a published flake ref.** `yo bootstrap` runs
  `nixos-rebuild` against `github:aka-rider/yolobox/v<version>`. The VM holds
  no checkout of this repo. See `docs/distribution.md`.
- **Secrets reach the agent only over reverse unix sockets.** Lima forwards
  the Mac's 1Password socket and the AWS broker's socket into the VM, and
  `systemd-socket-proxyd` hands each to the agent. Nothing sensitive is
  copied into the guest. See `docs/ssh.md` and `docs/aws.md`.

## Where things live

Mac side:

- `yo`: the CLI, one Python 3.9 file using only the stdlib. `yo.md`.
- `aws-broker`: the credential broker `yo` starts and stops on the Mac.
  `docs/aws.md`.
- `lima/yolobox.yaml`: instance config, read once at creation.
  `lima/yolobox.md`.
- `homebrew/yolobox.rb`: the brew formula, rendered into the tap on release.
- `templates/default/`: `devbox.json` and `.envrc` for a new project.

Box configuration (Nix):

- `flake.nix`: wires the modules together and threads the Mac username and
  the constant `agentUser` into them. `flake.lock` pins the inputs.
- `nix/base.nix`: filesystems, boot, sshd, tmpfiles, git defaults, the lima
  units and core packages. It declares both accounts, `users.users.${username}`
  and the agent's. `nix/base.md`, `docs/accounts.md`.
- `nix/luks.nix` and `nix/luks/`: the root disk's encryption. `luks.nix` is
  the initrd configuration, `apps.nix` defines the three scripts that run
  there. `docs/disk-encryption.md`.
- `nix/agentic.nix`: only an imports list for `harnesses.nix`,
  `herd-server.nix`, `display.nix` and `t3.nix`.
- `nix/harnesses.nix`: the `claude` launcher and the user service that
  installs claude, pi, opencode and agent-browser from their vendors.
  `nix/harnesses.md`.
- `nix/herd-server.nix`: the VM's own herdr server, run as the agent.
  `nix/herd-server.md`.
- `nix/display.nix`: the virtual X display, browsers and screen recording.
  `nix/display.md`.
- `nix/t3.nix`: a drop-in onto the vendor-installed t3code user unit.
  `nix/t3.md`.
- `nix/killswitch.nix`: the opt-in kill switch (armed by
  `yolobox.network.exitNode`): the nft table that rejects egress outside the
  tailnet exit node, the nameserver assertion and `/etc/yolobox/network.json`.
  `nix/killswitch.md`.
- `nix/tailscale.nix`: the tailnet, `tailscale serve` for t3 and, on a box that
  opted in to the kill switch, the exit-node selection.
  `nix/tailscale.md`.
- `nix/podman.nix`: rootless podman with a docker-compatible socket.
- `nix/lsp.nix`: Python language-server wiring for every editor.
- `nix/guest.nix` builds `nix/guest/yolobox-guest.sh` into the
  `yolobox-guest` package, shellchecked, on both accounts' PATH.
- `nix/lib/`: small shared definitions.
  - `agent-env.nix`: the agent's environment, applied everywhere.
  - `herdr-pkg.nix`: the herdr package selection shared by `harnesses.nix`
    and `herd-server.nix`.
  - `nix-ld-env.nix`: the two nix-ld variables a glibc binary needs.
  - `home-tmpfiles.nix`: tmpfiles rules that link files into a home.
  - `claude-hooks-file.nix`: the path of claude's settings file.
  - `basedpyright.nix`: the Python language-server definition.
- `nix/pkgs/`: packages nixpkgs lacks or lags on.
  - `far2l.nix` (`nix/pkgs/far2l.md`), `herdr-bin.nix` (the pinned herdr
    release asset) and `yolobox.nix` (`yo` itself, with the version baked in).

Release and repo plumbing:

- `.version`: the release version. The release workflow writes it, not you.
- `.github/workflows/`: `ci.yml` and `release.yml` both call `verify.yml`.
  `release.md` beside them explains the release job.
- `ruff.toml`: lint and format settings for `yo` and the tests.
- `TODO.md`: known problems and upstream PRs awaiting review. `upstream/`
  holds reports drafted and not yet filed.

Tests, all under `tests/`:

- `test_yo.py`, `test_yo_commands.py`, `test_yo_paths.py`: `yo`'s pure
  functions, argv builders and path handling.
- `test_aws_broker.py`: the broker's request handler.
- `test_consistency.py`: keeps the Python, Nix and shell spellings of one
  fact equal.
- `test_guest_script.py`: runs the guest script's functions under bash.
- `test_yo_luks.py`: the passphrase derivation and the ssh agent protocol.
- `test_luks_scripts.py`: runs the three initrd scripts under bash.

## Build, test, release

```sh
python3 -m unittest discover -s tests        # CI runs 3.9 and the newest 3.x
ruff check . && ruff format --check .
shellcheck -S warning nix/guest/yolobox-guest.sh
YOLOBOX_USERNAME=$(id -un) nix flake check --impure
```

- Python 3.9 is the floor because the Mac's Xcode Python is 3.9. The Mac
  usually has no nix, so check Nix changes inside the guest.
- The default configuration evaluates as it is. A box that opts in to the kill
  switch with `yolobox.network.exitNode` must also name
  `networking.nameservers`, both in `/etc/yolobox/local.nix`. To evaluate the
  opted-in variant anywhere else, go through `extendModules` with a stand-in,
  as the `eval` job in `.github/workflows/verify.yml` does (its `ARMED`
  expression). Inside a box that opted in through its `local.nix`, evaluating
  the plain un-armed configuration requires forcing
  `yolobox.network.exitNode = lib.mkForce null`, for example through
  `extendModules`.
- Nix needs `YOLOBOX_USERNAME=$(id -un)` and `--impure` on every command,
  because the flake has no pure way to learn the host username. `flake.nix`
  throws if it is unset.
- On a local clone, `git add` new files before running nix, which ignores
  untracked files in a git tree.
- A new `yolobox-guest` subcommand does not exist on a box until the box is
  rebuilt. Calling it earlier exits 64, and `yo` reports a version skew.
- Keep the literal line `YO_VERSION = "dev"` exactly as written. The Nix
  build and the brew formula both patch that text.
- A release is a GitHub release published from the tip of `main`. The
  workflow stamps `.version`, moves the tag onto that commit and updates the
  tap. See `.github/workflows/release.md`.

## Going deeper

`docs/README.md` states the convention and lists every story file. A story
lives in `<stem>.md` beside the file it explains, and cross-cutting ones live
in `docs/<topic>.md` (`accounts`, `ssh`, `aws`, `disk`, `disk-encryption`,
`mac-network`, `distribution`). Before editing a file, check whether its `<stem>.md`
exists and read it. A short "why" stays in the code comment; the story file
holds the incident, the evidence and how to recognise the failure.

## Host or guest?

Check before you run anything. `uname` prints `Darwin` on the Mac and
`Linux` inside the VM. `yo`, `limactl` and `~/.lima` exist only on the Mac.
`yolobox-guest` and `/etc/yolobox/version` exist only in the guest, which has
no `yo` at all. Commands here that name `yo ssh` or `yo enter` run from the
Mac and land in the guest.
