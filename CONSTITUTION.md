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
  must be restated in full because lima's yq cannot read the file. A unit
  test (`TestLimaYamlMatchesPortForwardsConstant` in `tests/test_yo.py`)
  keeps the checked-in file's socket-forward rules in sync with `yo`'s own
  `LIMA_PORT_FORWARDS` constant — that constant, not the yaml, is what a
  restate command is built from, so the two owe each other this check.

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

- ALWAYS reach the agent's 1Password through the lima reverse socket
  forward (`/run/yolobox-op/agent.sock`, operator-owned, proxied by
  `systemd-socket-proxyd` to the agent-owned `/run/yolobox/op-agent.sock`),
  never `ForwardAgent`. The reverse forward is set up once per hostagent
  start and lives with the VM, not with any one pane.
- NEVER put `ForwardAgent` in `~/.lima/yolobox/ssh.config`. That file is
  shared by every ssh role `yo` spawns, so a forward declared there would
  reach `yo code`, `yo zed` and every t3-spawned session, not only the one
  interactive command that asked for it.
- ALWAYS give `yo ssh`'s own `ForwardAgent` the 1Password socket path
  itself, and only over its own `ControlPath=none` connection — the
  operator's per-session forward, kept because the operator is not the
  agent and carries no reverse forward of its own. The default forwards
  `$SSH_AUTH_SOCK`, which on this Mac is Apple's empty agent.
- ALWAYS strip `IdentityAgent` and lima's `Include` when copying
  `~/.ssh/config` into the VM. Both name paths that do not exist there and
  take the forwarded agent away.
- ALWAYS spell git `includeIf` paths exactly as the project path is
  spelled on the Mac. `yo` mirrors the logical spelling into the VM
  verbatim, so any other spelling matches nothing there.
- NEVER copy a private key into the VM.

## AWS credentials

- ALWAYS fix a process's AWS profile with `AWS_PROFILE` against the
  guest's rendered `AWS_CONFIG_FILE`, never one global, switchable
  profile. botocore fixes a process's credential source at first use and
  only refreshes minutes before expiry, so flipping one shared profile
  mid-session leaves an already-running process on the old account until
  its next refresh, tens of minutes later.
- ALWAYS tie the AWS broker's lifetime to lima's hostagent
  (`--watch-pid <pid>`, that pid read from `limactl list yolobox --json`'s
  `hostAgentPID`, never by opening lima's own `~/.lima/yolobox/ha.pid`
  directly — lima rewrites that file on every start), never to a
  per-session forward or an idle timeout. Profiles are chosen per process,
  not per session, so there is no session left to bound the broker's life
  by.
- ALWAYS reach the AWS broker the same way as 1Password: a lima reverse
  unix-socket forward (`/run/yolobox-op/aws-broker.sock`, operator-owned,
  proxied by `systemd-socket-proxyd` to the agent-owned
  `/run/yolobox/aws-broker.sock`), never a forwarded TCP port. A live test
  showed a Mac-side VPN blackholing every guest connection to lima's
  gateway address while general egress kept working; a reverse unix
  socket rides lima's own ssh connection instead and is unaffected.
- NEVER guard a reversed unix socket with a bearer token. Filesystem
  permissions on the agent-owned socket are the entire authorization
  boundary — anything more is a second mechanism to keep in sync with the
  first for no added safety.
- ALWAYS keep the agent's environment (`SSH_AUTH_SOCK`, `AWS_CONFIG_FILE`)
  declared once, in `nix/lib/agent-env.nix`, applied identically to the
  herdr server, t3 and every agent login shell, rather than three copies
  left to drift apart. The same file's `awsBrokerSock` is the one place
  naming the guest-side broker socket path, read by `nix/base.nix`'s proxy
  unit and folded into the guest script's `YOLOBOX_AWS_BROKER_SOCK`
  (`nix/guest.nix`); a unit test (`TestAgentEnvMatchesYoSockets`) checks it
  against `yo`'s own `AGENT_AWS_BROKER_SOCK` constant, the same discipline
  as the `lima/yolobox.yaml` check above.
- NEVER let the broker write a secret to stdout after its handshake line,
  or to any log.

## Agents and the herd

- ALWAYS start an agent in a herdr pane on the saved `yolobox` machine,
  never expect `yo enter`'s plain landing shell to report to herdr. Only a
  real pty opened against the VM's own herdr server reaches its manifest.
- ALWAYS treat herdr compatibility as capability-gated, not
  version-equal. `herdr machine add` accepts a saved connection only when
  the guest's `endpoint_protocol_generation` matches the client's own
  constant exactly — coarser than the version string, so the VM and the
  Mac may run different herdr versions as long as the generation agrees.
  Bump `yolobox.harness.herdr.version`/`.hash` when it does not.
- ALWAYS keep `herdrPkg` in `environment.systemPackages`. `herdr machine
  add` adopts a running, compatible server rather than starting its own,
  and installs its own binary only when none turns up on its probe list —
  a list that includes `/run/current-system/sw/bin/herdr`. Drop the
  package and a fresh `machine add` tries to install over the system one
  instead of adopting it, and a non-interactive install is refused
  outright.
- ALWAYS keep `HERDR_AGENT=claude` exported in the claude launcher ahead
  of its `exec`. herdr classifies a pane's agent by its foreground
  process name, and the launcher execs a binary named after its version
  directory, not `claude` — without the hint, classification never
  reaches herdr's screen manifest and the session reports nothing, with
  no error anywhere.
- NEVER add a tmpfiles rule that deletes a herdr-installed integration
  file (`~/.pi/agent/extensions/herdr-agent-state.ts`, for one).
  `systemd-tmpfiles-resetup.service` reapplies `r` rules on every switch,
  not once, so the rule would silently un-install the integration on the
  very next switch.
- ALWAYS deliver hooks through the box-owned `~/.local/bin/claude`
  launcher's `--settings` file. The
  `/etc/claude-code/managed-settings.json` tier is discarded whole
  whenever the remote-settings tier is non-empty, and it always is here.
- NEVER let anything but the box own `~/.local/bin/claude`. That path is
  the only hook channel, and the agent's own dotfiles prepend the
  directory to PATH, so nothing on the system PATH can win the race
  against it. It is a tmpfiles `L+` link to `/etc/yolobox/bin/claude`,
  re-asserted by `yolobox-claude-launcher`; `claude update` leaves a
  custom launcher alone and is welcome.
- NEVER run `agent-browser install`, and NEVER let a vendor installer edit
  an rc file (`--no-modify-path`). The install downloads a glibc Chrome
  for Testing that cannot execute on NixOS, and an installer-written rc
  line changes PATH behind the box's back on the very path it owns.
- ALWAYS let pi own `~/.pi/agent/settings.json`. pi rewrites it and only
  logs a failed write, so a symlink there silently drops every installed
  package.
- ALWAYS install pi packages with `pi install`, never by editing its
  settings file.
- ALWAYS keep the VM free of a C and Python toolchain. It keeps native npm
  modules from compiling at install time; every package that would
  otherwise need one — t3, agent-browser — installs itself from a
  vendor-shipped prebuilt binary instead of a source npm package.
- NEVER set `PLAYWRIGHT_MCP_USER_DATA_DIR`. The box runs Playwright
  isolated per launch, and the server throws when both an isolated launch
  and a user-data-dir are set.

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
  the project checkout, so the push channel never sees stray binaries. The
  box's claude launcher exports a per-project `PLAYWRIGHT_MCP_OUTPUT_DIR`
  before exec, and a PreToolUse hook reroutes an explicit `filename` into
  that same directory, because Playwright resolves an explicit name
  against the checkout by design.
