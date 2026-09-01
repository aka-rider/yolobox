# Constitution

Rules for changing this repo. Each one was paid for by an outage; the
reason follows every rule so you can tell when it stops applying. The full
story behind each lives in `CLAUDE.md`.

## Before you start

Identify whether you are running on the host (MacOS) or Guest (Linux).

## The VM

- ALWAYS keep the VM rebuildable from this repo alone. It has no snapshots
  and is meant to be thrown away; anything that needs hand setup after
  creation is a defect.
- ALWAYS keep `nix/base.nix`'s mount layout identical to the shipped
  nixos-lima image (`/boot` is the ESP). Diverging means every fresh
  instance needs a manual mount migration before its first switch.
- ALWAYS keep `boot.loader.grub.configurationLimit = 1`. The ESP is 249 MiB
  and GRUB copies the incoming kernel before pruning old ones, so two
  retained kernels plus one incoming do not fit.
- ALWAYS keep `restartIfChanged = false` on `lima-init` and
  `lima-guestagent`. Restarting the guest agent kills every port lima
  forwards until the VM is stopped and started again.
- ALWAYS declare the operator's account as `users.users.${username}`, fed
  by `YOLOBOX_USERNAME=$(id -un)` and `--impure` — uid 501 (lima's own),
  home `/home/${username}.guest`, in `wheel`. A hardcoded name creates a
  second account with the same uid and splits state across two homes.
- ALWAYS treat `lima/yolobox.yaml` as read once, at creation. An existing
  instance is changed with `limactl edit` while stopped, and `portForwards`
  must be restated in full because lima's yq cannot read the file.

## Accounts

- NEVER give the agent account uid 501. uid 501 is lima's own cidata uid,
  reserved for the operator; a second account sharing it splits state
  across two homes exactly the way the hardcoded `xiii` account once did,
  with `whoami` and `SUDO_USER` unable to tell them apart.
- NEVER add the agent to `wheel`, to `nix.settings.trusted-users`, or to
  any sudoers rule, and NEVER make the agent lima's cidata account.
  `lima-init` runs `usermod -a -G wheel $LIMA_CIDATA_USER` unconditionally,
  after NixOS activation, with no `Condition=` guarding it — wheel
  stripped from that account is silently reinstated on the very next boot.
- ALWAYS let an ssh role, not a bare `User=` override, own the
  `ControlPath`. Lima's `ControlPath` carries no `%r`, so OpenSSH's mux
  client never re-checks the login user against an already-open master's,
  and `-o User=agent` layered onto the operator's live master silently
  runs as the operator.
- ALWAYS point `homeTmpfiles`, every systemd `User=`, and the direnv
  whitelist at `agentUser`, never at `${username}`. Review invariant:
  `grep -rn 'username' nix/` must hit only `nix/base.nix`.

## Distribution

- NEVER hand-edit `.version` — the release workflow writes it from the
  release tag. `yo` bakes the version in at package build time and derives
  its flake ref straight from it (`github:aka-rider/yolobox/v<version>`),
  so a wrong version ships a `yo` that builds the wrong system with no
  error anywhere.
- ALWAYS publish a release from a commit that is the tip of `main`. The
  release workflow force-moves the tag onto its stamp commit, so it refuses
  any release whose tag sits anywhere else — a release cut from a feature
  branch fails at that guard rather than republishing different content
  under a tag someone may already have fetched.
- NEVER let a VM hold a checkout of yolobox's own repo. `yo bootstrap`
  builds from a flake ref now, not a push; a guest checkout would reopen
  the exact push-to-checkout hazard this repo already carries for user
  projects, for a repo nothing in the VM reads any more.
- ALWAYS keep `aka-rider/yolobox` public. `nixos-rebuild` fetches the
  flake ref anonymously, with no credentials configured anywhere in `yo`;
  a private repo fails every `yo bootstrap` and every
  `/etc/yolobox/local.nix` rebuild with an auth error, on every box in the
  field at once.

## Building

- ALWAYS `git add` before `nixos-rebuild` when hacking on a local clone.
  Nix evaluates the git tree, so an untracked file does not exist to it.
  This does not apply to the VM itself any more: it builds from a
  published flake ref, not a checkout, so there is no local git tree for
  it to evaluate at all.
- ALWAYS run `nixos-rebuild switch` unpiped, and read its exit code. A
  pipe through `tail` hides a failed bootloader install as exit 0.
- ALWAYS check `df /boot` and compare `readlink /run/current-system` with
  `readlink /nix/var/nix/profiles/system` when a switch behaves strangely.
  Disagreement means the box runs one generation and the profile claims
  another; do not garbage-collect in that state.
- ALWAYS use the `path` option, not `environment.PATH`, in a systemd unit.
  NixOS already defines the latter for every service, so a second
  definition is a conflict.
- ALWAYS write `${pkgs.xvfb}/bin/Xvfb` explicitly. `pkgs.xorg.xvfb` is
  deprecated and `lib.getExe pkgs.xvfb` points at a binary that does not
  exist.
- NEVER add a tmpfiles rule for `/tmp/.X11-unix`; systemd ships one and a
  duplicate logs errors every boot.

## Disk

- ALWAYS diagnose a full disk from the Mac with `yo gc`. In-VM tools die
  first, while `yo ssh` still works because it reaches in from the Mac, and
  the operator's `sudo` still works there because ext4 reserves 4.3 GB for
  root — the agent has no `sudo` at all, so a stuck agent session proves
  nothing about disk space either way.
- ALWAYS reclaim before growing. A full disk has so far always been one
  project's build output, never a real need for more space.
- ALWAYS gate the deletion of a project build directory on `git
  check-ignore`. Deleting a tracked directory dirties the checkout and
  silently refuses every later push into it.
- ALWAYS switch to a generation carrying `boot.growPartition` before
  `limactl edit --disk`. Otherwise the backing file grows and the guest
  partition stays where it was, with no error anywhere.

## Identities and SSH

- ALWAYS give `ForwardAgent` the 1Password socket path itself. The default
  forwards `$SSH_AUTH_SOCK`, which on this Mac is Apple's empty agent.
- ALWAYS forward the 1Password agent only from the interactive `enter`/`ssh`
  commands, each on its own `ControlPath=none` connection. No shared,
  persistent `ControlMaster` may carry it, or `yo code`/`yo zed`/a bare
  `ssh` inherit it, and a lingering `ControlPersist` master would leave the
  agent socket alive in the VM after the session ends.
- ALWAYS strip `IdentityAgent` and lima's `Include` when copying
  `~/.ssh/config` into the VM. Both name paths that do not exist there and
  take the forwarded agent away.
- ALWAYS spell git `includeIf` paths exactly as the project path is
  spelled on the Mac. `yo` mirrors the logical spelling into the VM
  verbatim, so any other spelling matches nothing there.
- NEVER copy a private key into the VM.

## AWS credentials

- ALWAYS let the host-side broker (`aws-broker`), never a flat env var,
  carry AWS secrets into the guest. Explicit env creds outrank the
  container credential provider in botocore's chain, so a flat var sitting
  alongside the broker's URL would win and silently defeat its refresh.
- NEVER let the broker write a secret or the bearer token to stdout after
  its two handshake lines, or to any log.

## Agents and the herd

- ALWAYS keep the VM's herdr version equal to the Mac's Homebrew herdr.
  The wire protocol moved across a patch release; a mismatch rejects every
  report silently.
- ALWAYS deliver hooks through the `claude` wrapper's `--settings` file.
  The `/etc/claude-code/managed-settings.json` tier is discarded whole
  whenever the remote-settings tier is non-empty, and it always is here.
- ALWAYS start agents that must appear in the herd with `yo enter`. Only
  it forwards the socket and sets the herd env; `yo ssh`, `yo code` and
  `yo zed` do not.
- ALWAYS prove herd reporting with an interactive session or `yo
  herd-check`. A headless `claude -p` registers and releases within two
  seconds, which looks identical to hooks that never ran.
- ALWAYS let pi own `~/.pi/agent/settings.json`. pi rewrites it and only
  logs a failed write, so a symlink there silently drops every installed
  package.
- ALWAYS install pi packages with `pi install`, never by editing its
  settings file.
- ALWAYS keep the VM free of a C and Python toolchain. It keeps native npm
  modules from compiling at install time; packages that need it are built
  by nix (`nix/pkgs/t3.nix`) instead.
- NEVER add age-based tmpfiles cleanup for sockets in `/run/yolobox`. A
  live connection never touches the file, so an age would reap a working
  pane's socket.

## Paths

- ALWAYS derive a guest path by mirroring the Mac's logical `$PWD` (or a
  repo's logical toplevel) relative to `$HOME` onto the guest `$HOME`.
  Logical, never `realpath` or `cd -P`: physical resolution loses the
  spelling the user stands in, and a hardcoded root splits state across
  two trees the way a hardcoded username once split it across two homes.
- NEVER let a `yo` command target a guest path outside the guest `$HOME`.
  A path the mirror cannot place lands at the guest home with a loud
  stderr notification instead of a silent guess.

## Project repos in the VM

- ALWAYS keep a project's VM checkout clean before pushing to it. The push
  target is `updateInstead`, which refuses when the worktree or index
  differs from HEAD.
- ALWAYS pull the VM's commits (it commits `devbox.lock`) before pushing
  again, or the push lands on a diverged branch and is refused. That pull
  is also the sandbox's trust boundary: review the agent-authored commits
  before running anything they contain.
- ALWAYS keep Playwright output in `~/artifacts/<project>/`, never inside
  the project checkout, so the push channel never sees stray binaries.
