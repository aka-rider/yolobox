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
commit `4c154fc`.

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
- **Git is the bridge.** `yo link` makes the VM side of a repo a
  push-to-checkout target (`receive.denyCurrentBranch = updateInstead`), so
  the VM refuses a push when its worktree differs from HEAD. The VM commits
  `devbox.lock`, so pull before you push again.
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
- `nix/tailscale.nix`: the tailnet and `tailscale serve` for t3.
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

## Build, test, release

```sh
python3 -m unittest discover -s tests        # CI runs 3.9 and the newest 3.x
ruff check . && ruff format --check .
shellcheck -S warning nix/guest/yolobox-guest.sh
YOLOBOX_USERNAME=$(id -un) nix flake check --impure
```

- Python 3.9 is the floor because the Mac's Xcode Python is 3.9. The Mac
  usually has no nix, so check Nix changes inside the guest.
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
in `docs/<topic>.md` (`accounts`, `ssh`, `aws`, `disk`, `mac-network`,
`distribution`). Before editing a file, check whether its `<stem>.md`
exists and read it. A short "why" stays in the code comment; the story file
holds the incident, the evidence and how to recognise the failure.

## Host or guest?

Check before you run anything. `uname` prints `Darwin` on the Mac and
`Linux` inside the VM. `yo`, `limactl` and `~/.lima` exist only on the Mac.
`yolobox-guest` and `/etc/yolobox/version` exist only in the guest, which has
no `yo` at all. Commands here that name `yo ssh` or `yo enter` run from the
Mac and land in the guest.
