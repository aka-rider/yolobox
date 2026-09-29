# yolobox — notes for whoever works on this repo next

This repo describes one NixOS VM on a Mac, run by Lima, where coding agents
work with no host mounts and git as the only bridge. `README.md` gets a
person from nothing to a working project. This file holds the reasoning
behind the design and the shape of each failure we have met, so you can
recognise one instead of rediscovering it.

Read `CONSTITUTION.md` before changing anything. It is the short list of
rules this file justifies.

## Where things live

- `yo` — the Mac-side CLI, a single Python 3.9 file with no dependencies
  beyond the stdlib. Everything the Mac does to the VM goes through it;
  `yo --help` lists each command, and `yo <command> --help` gives that
  command's fuller prose.
- `flake.nix` — wires the modules together and threads the host username
  in.
- `nix/base.nix` — filesystems, boot, sshd, tmpfiles, git defaults, the
  lima units, core packages.
- `nix/podman.nix` — rootless podman with a docker-compatible socket.
- `nix/harnesses.nix`, `nix/agentic.nix` — the coding agents: the
  box-owned `claude` launcher, the user service that installs claude, pi,
  opencode and agent-browser from their vendors, the user path unit that
  re-asserts the launcher, and the herdr package selection this file
  shares with `nix/herd-server.nix`.
- `nix/herd-server.nix` — the VM's own herdr server, run as the agent
  account from boot.
- `nix/lib/agent-env.nix` — the one attrset declaring the agent's
  environment (`SSH_AUTH_SOCK`, `AWS_CONFIG_FILE`), applied identically to
  the herdr server, t3 and every agent login shell.
- `nix/display.nix` — the virtual X display, browsers, screen recording.
- `nix/lsp.nix` — the Python language-server wiring for every editor.
- `nix/t3.nix` — t3code's environment: a drop-in onto the vendor-installed
  user unit (`nix/lib/agent-env.nix`, nix-ld), and the path unit that prunes
  old versions. Installed and updated by `yolobox-harness-install`
  (`nix/harnesses.nix`), the same as claude and pi.
- `nix/lib/nix-ld-env.nix` — the two nix-ld env vars a dynamically linked
  glibc binary needs, shared between `nix/harnesses.nix`'s install script
  and `nix/t3.nix`'s drop-in.
- `nix/tailscale.nix` — the tailnet, and the oneshot that puts t3 on it
  through `tailscale serve` instead of the firewall.
- `nix/guest.nix`, `nix/guest/yolobox-guest.sh` — the guest-side half of
  `yo`'s shell (project walk, gc tiers, AWS probe and the rest), built and
  shellchecked as one package, `yolobox-guest`, on both accounts' PATH.
- `nix/pkgs/` — packages nixpkgs lacks or lags on.
- `nix/pkgs/far2l.nix` — far2l with every NetRocks backend, S3 included,
  built TTY-only; see "far2l: TTY-only, S3 through the broker" below.
- `lima/yolobox.yaml` — read once, when the instance is created.
- `templates/default` — `devbox.json` and `.envrc` for a new project.
- `homebrew/yolobox.rb` — the brew formula, with `@URL@`/`@SHA256@` holes;
  `release.yml` renders it into `aka-rider/homebrew-tap` on every published
  release.
- `tests/test_yo.py` — unit tests for yo's pure functions and argv builders;
  `python3 -m unittest discover -s tests`.
- `TODO.md` — known problems, including the upstream reports still owed.

## Two accounts: the operator mirrors the host, the agent does not

Until now this VM had one human-and-agent account. It now has two, split
so that every AI coding session runs with no path to root.

Lima creates a guest account named and uid-matched after the Mac account
that started the VM — that account is the **operator's**, and only the
operator's. `flake.nix` declares it as `users.users.${username}` rather
than inventing one, and since Nix has no pure way to learn the name,
`YOLOBOX_USERNAME=$(id -un)` plus `--impure` carries it in on every
`nixos-rebuild`. The operator is the only account in `wheel`, so it is the
only one `security.sudo.wheelNeedsPassword = false` covers, and it is the
account behind `yo ssh`, `yo disk-grow`, `yo bootstrap`, `yo gc`'s machine tier, and any
manual `nixos-rebuild`.

Every AI coding session runs as a second account instead: `agent`, a
constant in `flake.nix`, never threaded in from the host. Its uid is 1000,
its home is `/home/agent`, its groups are `users` and `systemd-journal`,
and it is in neither `wheel` nor any sudoers rule — it has no `sudo` at
all. `yo enter`, `yo code`, `yo zed`, and every t3-spawned session land
there.

The agent's uid is 1000, not 501, because of an outage this box already
lived through once. An earlier revision hardcoded `xiii`, an old Mac
account name, as the box's *only* guest account. When the Mac account
changed, `xiii` lived on as a second account with the same uid but its own
`/home/xiii.guest`, and every module storing state under
`config.users.users.xiii.home` wrote there instead of the real home.
`whoami` and `SUDO_USER` could not show it, because they resolve a uid to
whichever passwd entry comes first. Retiring `xiii` from every module in
one switch was enough: `nixos-rebuild` removes a user it once declared and
no longer does. uid 501 is lima's own cidata uid, reserved for the
operator; giving `agent` that same uid would reopen exactly that failure
mode, silently splitting state across two homes the way `xiii` once did —
which is why `agent` gets uid 1000 instead, an ordinary unprivileged uid
lima has no opinion about.

Splitting the account was not optional once "the agent cannot reach root"
was the goal, because of one lima behaviour verified directly in the
running VM: `lima-init`'s start script runs
`usermod -a -G wheel $LIMA_CIDATA_USER` unconditionally on line 22 — only
the `useradd` above it is guarded by a conditional — inside a
`Type=oneshot` unit with no `Condition=` anywhere, and it runs *after*
NixOS activation has already applied that switch's group list. So stripping
`wheel` from lima's own account is silently reverted at the very next
boot: the box would look hardened in the config and would not be hardened
in fact. That is exactly why the agent is a brand-new account lima has
never heard of, rather than the pre-existing account with its privileges
stripped — lima can only re-grant `wheel` to the account it created, and
it has never created `agent`. The design fails closed from here: if
anything about the agent account is set up wrong, the agent cannot log in
at all (loud), rather than silently landing with root anyway.

That fail-closed shape fired on the very first box built from a release
(v0.9.1, 2026-09-02): `yo bootstrap` built and rebooted fine and then
died at `yo: cannot ssh into yolobox as 'agent'`, with `Permission denied
(publickey)` and nothing in the sshd journal. `nix/base.nix` had pointed
the agent's `AuthorizedKeysFile` at `/etc/ssh/authorized_keys.d/<operator>`,
the file lima-init rewrites every boot, on the theory that a root-owned
file is one the agent cannot tamper with. True, and also the reason it
could never work: lima-init writes it 0600 root inside a 0700 root
directory, and sshd opens an `AuthorizedKeysFile` with the *target user's*
uid, so `agent` gets `EACCES` and no key is ever offered a match
(`sudo -u agent cat` on the file reproduces it). The operator never hit
this because lima-init also writes the same key into
`~/.ssh/authorized_keys`, and sshd's default list checks that first. The
box now reads the file through an `AuthorizedKeysCommand` run as root; the
command is a copied `/etc/ssh/agent-authorized-keys` rather than a store
path because sshd's `safe_path` refuses any command whose canonical path
has a group-writable component, and `/nix/store` is `1775`.

The system journal turned out to gate on the same group. `getfacl` on
`/var/log/journal/*/system.journal` shows `group:wheel:r--`,
`group:adm:r--`, `other::---` — readable only through `wheel`'s ACL, with
no other route in. `xvfb` and `openbox` are system units that run *as* the
agent, so their logs land in the system journal, not some user journal the
agent already owns by default. Hence the agent carries `extraGroups = [
"systemd-journal" ]` on top of dropping `wheel` — without it, `journalctl
-u openbox` silently narrows to only the agent's own messages. t3code is
not part of this any more: it is a vendor-installed *user* unit (see "t3:
vendor-installed and self-updating, like claude and pi" below), so its
logs were never gated by this group to begin with — they sit in the
agent's own user journal, which the agent already owns outright.

Two accounts sharing one VM also exposed a trap in lima's ssh
multiplexing. Lima's `ControlPath` carries no `%r` in its template, and
OpenSSH's mux client never re-checks the login user of a new connection
against the master's — it just reuses whatever session the master already
authenticated. So `-o User=agent` issued over a `ControlMaster` the
operator's session already opened silently runs as the **operator**, not
the agent, with no error from ssh at all. Recognise it by a session that
looks separated — the shell prompt names `agent`, the command line said
`-o User=agent` — but `id` still reports uid 501 and every file it touches
lands under `/home/${username}.guest` rather than `/home/agent`. The fix
is structural, not a flag: an ssh role owns the `ControlPath` itself (a
distinct socket per role, e.g. `~/.lima/yolobox/ssh-agent.sock`), so the
two accounts never share a multiplexed master to begin with.

That block now lives in `~/.lima/yolobox/ssh.config` itself, written by
`yo` above lima's own `Host lima-yolobox` block, rather than hand-written
into `~/.ssh/config`. It is a safe place to put it because lima writes
that file but never reads it back — its own header says so — so nothing
lima does can be confused by a block lima did not author, and because ssh
resolves `Include` recursively and keeps the first value it sees per
keyword: the `Match` at the head of the included file still wins over
lima's own `Host` block further down the same file, exactly as it won
when it lived above the `Include` line in `~/.ssh/config`. What forces
`yo` to keep re-applying it rather than writing it once is a fact verified
directly against the running VM: lima rewrites `ssh.config` on every
`limactl start` — its mtime tracks the last start, the same second as
`ha.pid` and `cidata.iso`, while `lima.yaml` and `lima-version` stay older
— so a one-shot write would be silently undone by the very next `yo up`.
`yo` re-applies the block idempotently from two places instead: `ssh_base()`,
which every ssh `yo` spawns funnels through, so the file is repaired
before any connection, `yo code`/`yo zed` included; and the end of
`cmd_up()`, because `limactl start` is what clobbers the file and `cmd_up`
opens no ssh of its own to trigger the `ssh_base()` repair. `~/.ssh/config`
itself now needs only one line, `Include ~/.lima/yolobox/ssh.config`, and
`yo bootstrap`/`yo code`/`yo zed` offer to add it when it is missing and
stdin is a tty.

This still leaves a gap, and it is worth stating plainly rather than
papering over: nothing in lima or ssh enforces the split at the moment a
connection is made. `limactl start yolobox` run by hand, followed by
launching Zed directly with no `yo` command in between, still opens the
project as the operator, because nothing has re-applied the `Match` block
or even required it to exist. Any `yo` command that touches the VM heals
this the next time it runs, but the box can sit in the unhealed state
until then.

This narrows what an agent can do, but it does not "isolate" or "contain"
it, and this file would be lying if it said otherwise. Left untouched by
the split: 1Password is reachable by every agent session for as long as
the VM runs, not merely during one `yo enter` — the reverse socket forward
lives with lima's hostagent, not with any one pane, so any process in the
VM can authenticate to GitHub as the operator (see "SSH identities and the
two GitHub accounts" below); the AWS broker likewise hands out real
credentials for every allow-listed SSO profile to any process that asks,
by design, not only for the duration of a session that opted in; every
commit an agent makes still rides the git push channel back to the Mac,
where the operator is the one who builds and
runs what it wrote; rootless podman's containment still rests on the
kernel's unprivileged user-namespace support holding, so a kernel exploit
reaches exactly as far as it did before; anything an agent leaves under
`/home/agent` persists across sessions with no clean state short of
recreating the VM outright; and `/etc/yolobox/local.nix` can put `wheel`
back in one line, because nothing inside the box enforces this separation
at runtime — only the discipline `CONSTITUTION.md` writes down does. The
honest summary: the agent no longer has root by construction and cannot
obtain it without a kernel exploit. That is a real narrowing. It is not
"the agent is contained".

Lima >= 2.1.0 names the guest home `/home/<user>.guest` (it was
`/home/<user>.linux` before; lima-vm/lima#4578). This box was brought up on
limactl 2.2.0. v1 of yolobox, the Docker-based predecessor, lives at commit
`4c154fc`.

## SSH identities and the two GitHub accounts

1Password is available to every agent session for as long as the VM
runs, not only inside the one pane that happened to start it. Lima itself
carries the forward now: `lima/yolobox.yaml`'s `portForwards` declares a
reverse unix-socket rule, `{guestSocket: /run/yolobox-op/agent.sock,
hostSocket: "<1Password's agent.sock>", reverse: true}`, alongside the t3
and dhcp rules and a sibling reverse rule for the AWS broker's own socket
(see "AWS credentials" below; `CONSTITUTION.md`'s "ALWAYS treat
`lima/yolobox.yaml` as read once" already covers restating this array in
full on an existing instance). Lima's hostagent dials that socket itself,
once, at every `limactl start`/`yo up`, over its own ssh master, as the
operator — no `yo` command has to be running, and no pane has to stay
open, for the forward to exist.

`nix/base.nix` gives it somewhere to land before lima ever tries: a
tmpfiles rule, `d /run/yolobox-op 0700 ${username} users -`, runs through
`systemd-tmpfiles-setup` before `sysinit.target`, well ahead of sshd and
lima's own forward, and `-resetup` recreates it on every `switch` too.
Verified directly against the running VM: lima's hostagent runs `rm -f
/run/yolobox-op/agent.sock` and then `ssh -O forward -R
/run/yolobox-op/agent.sock:<1Password socket> lima-yolobox` over its own
master exactly once, at hostagent start; a failure there is only logged,
nothing retries it, and nixos-lima's `lima-init` never runs lima's own
`/run/host-services` boot script, so — unlike a stock lima guest — nothing
else creates that directory either. The socket lands owned by the
operator, mode 0600 (`StreamLocalBindMask 0177`), because lima's hostagent
connects as `${username}`.

The agent cannot reach an operator-owned socket directly, so
`nix/base.nix` proxies it: `systemd.sockets.yolobox-op-agent` listens on
`/run/yolobox/op-agent.sock` with `SocketUser=agent`, `SocketMode=0600`,
wanted by `sockets.target`, so the agent-facing socket exists from boot
with no window where it is missing or wrongly permissioned; the matching
service execs `systemd-socket-proxyd /run/yolobox-op/agent.sock` as the
**operator** (`User=${username}`, never root), since the operator is the
account that can already read the target. `nix/lib/agent-env.nix` is the
one place that then points the agent at it —
`SSH_AUTH_SOCK=/run/yolobox/op-agent.sock` — applied identically to the
herdr server, t3, and every agent login shell (see "Where things live" and
"AWS credentials" below), so a real 1Password socket is simply what
`SSH_AUTH_SOCK` already resolves to everywhere an agent session starts.
`cmd_enter` arranges no forward of its own any more; landing in the guest
is the whole of what it does here.

`ForwardAgent` itself never goes into `~/.lima/yolobox/ssh.config`, the
file every `yo`-spawned ssh connection reads via `-F` (see "Two accounts"
above): that file is shared by every ssh role `yo` spawns, so a
`ForwardAgent` sitting there would reach `yo code`, `yo zed` and every
t3-spawned session too, not only the one command that asked for it. `yo
ssh`, the operator's own bare shell, is the one command left that still
forwards `ForwardAgent` per session — the 1Password socket path itself,
since the default forwards `$SSH_AUTH_SOCK`, Apple's launchd agent holding
nothing on this Mac — and only over its own `ControlPath=none` connection,
so that forward dies with the one pane that opened it rather than riding
a shared master into a session that never asked to carry it.

Because lima never re-establishes its side of a reverse forward — a
failure is logged once, at hostagent start, and nothing about it retries
— `yo` heals every reverse forward itself, 1Password's and the AWS
broker's alike (see "AWS credentials" below), through one mechanism
rather than one hand-written probe per socket. A `ReverseForward`
dataclass names each one — a label, the guest path, the Mac-side path, a
probe command run as the agent, and an `alive(proc)` callable — and two
constructors build the two instances: `one_password_forward()` (`ssh-add
-l`, judged by `op_probe_alive`) and `aws_broker_forward()` (`curl
--unix-socket /run/yolobox/aws-broker.sock http://broker/health`, alive on
exit 0 alone).

`op_probe_alive` is not "alive on exit 0 or 1" — ssh-add exits 1 for two
different reasons that must not be conflated. Alive is exit 0, or exit 1
*with* "The agent has no identities" in stdout — a reachable agent that
simply holds no keys yet. A **dead** forward also exits 1, but with
"communication with agent failed" instead, because the agent-side systemd
socket (`yolobox-op-agent.socket`) always accepts the connection whether
or not lima's forward and the Mac's own agent are alive behind it — the
`accept()` succeeds and only the SSH-agent protocol exchange after it
fails, so the exit code alone cannot tell the two apart and the exact
stdout wording is load-bearing. Recognise a dead 1Password forward by
that wording, not by ssh-add's exit code. `yo status` runs this exact same
`op_probe_alive` check on the same probe, not a re-implementation of it,
so the two can never quietly disagree about what counts as "up".

`heal_reverse_forward()` probes one entry as the agent first and, only on
a dead probe, re-issues exactly what lima's hostagent would have, over
the operator's own `ssh_base(OPERATOR, mux=True)` connection: `-O cancel`
first (its failure tolerated — there may be no live master, or no forward
registered on it yet, to cancel), then a plain `rm -f <guest path>` (not
an `-O` control command, so — via `ControlMaster auto` in lima's own ssh
config for `Host lima-yolobox` — issuing it is also what revives the
master if it had died), then `-O forward -R <guest path>:<host path>` —
before probing again and raising a `YoError` naming `limactl stop yolobox
&& yo up` if it still refuses.

`ensure_op_forward(aws_enabled)` runs this per forward through
`heal_forward_collecting_errors()`, which also catches a `YoError` raised
out of `heal_reverse_forward()` itself (e.g. the final `-O forward`
failing outright) and appends it rather than letting it escape uncaught —
one forward's hard failure must not abort the other's healing. The two
services are independent and optional, not one bundled decision: a
missing Mac-side 1Password socket (1Password simply not running) is only
a stderr note, never an error, and 1Password's heal is skipped for that
reason alone — never because AWS happens to be disabled. The AWS broker's
heal runs only when `aws_enabled` is true, i.e. only once
`ensure_aws_broker()` has itself started (or found already running) a
broker that answered its handshake. Failures from `ensure_aws_broker()`
and from either forward are collected into one list and raised together,
once, at the end, naming `limactl stop yolobox && yo up` as the recreate.

`vm_services()` decides whether any of this can even run before trying.
`account_status()` runs first: a status of `"unreachable"` — the operator
account itself cannot be ssh'd into, so the VM is not up at all — is a
stderr note and a return, nothing further attempted. Being reachable as
at least the operator is not enough on its own: `vm_units_present()` then
runs `systemctl cat yolobox-op-agent.socket` in the VM, and a box that
predates the proxy socket units answers with a nonzero exit — in which
case `vm_services()` prints `UNITS_MISSING_NOTE` ("box predates the
1Password/AWS forwards; run `yo bootstrap`") and returns. This replaces an
earlier design that gated the same skip on whether the *agent account*
itself was reachable yet: the real gate is the systemd units' presence,
not the account, since an older box's agent account can be perfectly
reachable while still lacking these units entirely. Past both checks,
`ensure_aws_broker()` runs first and then `ensure_op_forward()`, as
above.

`vm_services()` runs at the end of `vm_up()` by default (reached by
`cmd_up` and `cmd_disk_grow`). `cmd_bootstrap` does not take that default:
it calls `vm_up(run_services=False)`, because at that point in a fresh
bootstrap the proxy units may not exist yet and the nixos-rebuild that
creates the agent account may not have landed — then, once the
rebuild/reboot decision has fully settled (whether or not that needed a
`limactl restart`), it calls `vm_services()` itself exactly once, never
twice. `yo status` on a box with the proxy units prints the 1Password
probe status and the proxy socket unit statuses; on an older box without
them prints `UNITS_MISSING_NOTE`. It always prints a lima line from
`lima_config_gaps()`, and catches `YoError` from `aws_allowlist()` to
print "aws: config unreadable: ..." when the AWS config cannot be read.

`lima_config_gaps()` is the other half of keeping the two in sync, and it
no longer refuses anything. Before ever starting the VM, `vm_up()` asks
`limactl list yolobox --json` for the running instance's own
`config.portForwards` — never a text search of `lima/yolobox.yaml`
itself, which is only ever read once, at creation (see "The VM" section
of `CONSTITUTION.md`) — and looks, for each of `OP_GUEST_SOCK` and
`AWS_BROKER_GUEST_SOCK`, for some entry whose `guestSocket`, `hostSocket`
and `reverse: true` all match. A gap here used to abort `vm_up()`
outright; now `lima_config_gap_message()` is only a stderr note, naming
the full `LIMA_PORT_FORWARDS` array (the very constant `yo` renders for a
brand-new instance) as the exact `limactl edit --set '.portForwards =
...'` argument to restate, and `vm_up()` carries on regardless. It can
afford to, because the gap this check reports is only that *lima itself*
has no declared rule to re-establish the forward at the next hostagent
start — `heal_reverse_forward()` (above) adds the forward over lima's own
live ssh master with a bare `-O forward -R <guest>:<host>`, which needs no
matching entry in lima's `portForwards` array at all, so the forward
still comes up this session either way. The restated array is what makes
that permanent — it survives the next hostagent restart with no `yo`
needed to heal it again — not a precondition for the forward working
right now. No instance at all (`limactl list` itself failing) prints
nothing, since a brand-new box has no gap to report; the check exists for
an instance created before a rule existed, exactly the migration
README.md describes: a box whose forwards still carry only the t3, dhcp
and 1Password rules because the AWS broker's rule did not exist yet when
that instance was first created.

Two GitHub accounts share `github.com`, so the account is chosen by which
key is offered. The Mac does that with a `Host github-iurii-tech` alias
whose `IdentityFile` names a public key — a selector, not a key — and
`~/.dotfiles/gitconfig` rewrites `git@github.com:brocc-ab/` to that alias.
`yo seed` copies `~/.ssh/config` and every `*.pub` in, minus lima's
`Include` and `IdentityAgent`, both of which name paths that do not exist
in the VM and would take the forwarded agent away.

`yo seed` also copies group `.gitconfig` files. It asks the guest which
non-hidden top-level directories exist under its home, and for each one
that also exists under the Mac `$HOME` (a symlink to a directory counts)
it copies every `<root>/<group>/.gitconfig` with its `$HOME`-relative
path preserved. Those files sit next to repos, not inside one, so no push
ever carries them, and without them the `includeIf` in
`~/.dotfiles/gitconfig` matches nothing and work repos commit under the
personal email. That include must spell the path exactly as the project
is spelled on the Mac, because the mirror carries the spelling into the
VM verbatim. Seed creates no guest directory, so on a fresh box it copies
nothing until the first `yo link` grows a tree — re-run `yo seed` after
that first link.

How to recognise the class: `ssh -T git@github.com` and `ssh -T
git@github-iurii-tech` in the VM must greet two different names. "Could
not resolve hostname" means the ssh config never arrived; the wrong name
means the public keys did not.

## The mirror: how a Mac path becomes a guest path

`yo enter`, `yo code` and `yo zed` land in the guest twin of the Mac's
logical `$PWD`: the path relative to the Mac `$HOME`, grafted onto the
guest `$HOME`, subdirectory included. Logical means `$PWD` as the shell
spells it — a symlinked spelling is mirrored, never resolved, so the
guest sees whatever name you `cd` through on the Mac. `yo link` maps a
repo's logical toplevel the same way and refuses a repo outside `$HOME`.
When the mirrored path does not exist in the VM, the session lands at
the deepest existing ancestor; when the Mac cwd is outside `$HOME`, it
lands at the guest home. Both cases print exactly
`path "%s" does not exist in the yolobox you are in "%s"` to stderr, and
no `yo` command can target a guest path outside the guest `$HOME`. The
fzf picker behind `yo enter <fuzzy>` lists git repos anywhere under the
guest home, hidden directories pruned. `yo bootstrap` needs no mirrored
path at all any more: it builds the VM straight from a flake ref
(`github:aka-rider/yolobox/v<version>`), so there is nothing of yolobox's
own to locate inside the guest.

`yo link` no longer refuses outright the moment a Mac repo already carries
a `yolobox` remote. It reads the existing URL first: an unrelated remote —
one whose host is not `lima-yolobox` — is still refused, but a `yolobox`
remote whose host is `lima-yolobox` and whose path is merely stale is
updated in place with `git remote set-url`, and `yo` prints both the old
and the new URL to stderr so the change is visible rather than silent.
This is exactly the shape a recreated box leaves behind: every repo linked
against a previous instance carries something like
`ssh://lima-yolobox/home/xiii.guest/wrk/rune`, from before the
operator/agent split moved projects to `/home/agent`, and before this fix
the remedy was a manual `git remote remove yolobox` per repo before the
next `yo link` would run at all.

## `yo cp`: scp, not `limactl cp`

`limactl cp` cannot move a file into a project the agent then has to
write to. It copies as the **operator** (uid 501), so a file it writes
lands owned by an account `agent` cannot write as, and the operator's
shell on this box is unconfigured — its default zsh prompt writes onto
the same channel the copy's own rsync/scp handshake reads, which breaks
the transfer before a byte moves. `rsync` is not installed in the guest
at all, so that was never a fallback either. `yo cp SRC... DST` replaces
both: scp, run as the agent over the same `ssh_base` every other `yo` ssh
spawn goes through, so the ownership problem never arises, and no guest
shell of any kind runs, so the prompt problem cannot either.

scp gets that "no shell runs" property for free rather than `yo` having
to arrange it. The Mac ships OpenSSH 10.3, whose `scp` speaks the sftp
protocol by default, so sshd answers a `yo cp` connection by spawning
`sftp-server` directly — no login shell, agent's or operator's, runs at
any point. `nix/base.nix` leaves NixOS's default `allowSFTP = true`
untouched, and that default is the whole reason this works; turning sftp
off would break `yo cp` with no other symptom to point at. The command
also carries no `yolobox-guest` subcommand — unlike every subcommand
routed through the guest helper (see "The guest helper" above), it needs
no box rebuild first and works against a box of any version.

The guest side of an operand always starts with `:`, and `yo` never
guesses which side is which. `:` alone is the guest twin of the Mac's
logical current directory, the same mirror `yo enter` uses; `:rel/path`
extends that twin; `:~` and `:~/x` are the agent's home outright;
`:/abs/path` is an absolute guest path. Every one of these, relative or
absolute, is checked against the guest home the same way every other
`yo` command's guest path is (see "The mirror" above): a path that
resolves outside `/home/agent` is refused rather than silently placed
there. A copy has no existing-ancestor fallback the way a missing
mirrored directory does, so `yo` requires every source on one side and
the destination on the other and refuses anything else outright — no
colon anywhere, sources on both sides, or a copy entirely inside the
guest — each with a message naming the remedy, plain `cp` or `yo enter`.

Two refusals are worth recognising on sight. A relative guest spelling —
`:` or `:x` — issued from a Mac cwd outside `$HOME` has no mirror to
extend, so it is refused with `:~/...` named as the fix, the same
outside-`$HOME` case "The mirror" above describes for `yo enter`. And a
guest path that resolves outside `/home/agent`, absolute or relative, is
refused the same way `guest_path` refuses it everywhere else in `yo`.
The multi-source case is scp's own rule surfacing through `yo`, not a
rule `yo` invented: scp itself requires the destination to be a
directory once more than one source is given, which is why `yo cp` runs
`mkdir -p` against the destination itself — rather than its parent, the
single-source rule — whenever there is more than one source, DST is `:`,
or DST ends in `/`.

## Per-project dev shells

A project must be a git repo for one reason: the push channel. `yo link`
git-inits the VM side, so linked projects are correct by construction; a
project born in the VM needs `git init` first.

The VM checkout is a push-to-checkout target and refuses a push when the
worktree or index differs from HEAD. `nix/base.nix` sets
`receive.denyCurrentBranch = "updateInstead"` in `/etc/gitconfig`, so this
holds for every repo in the VM. devbox never touches the index, so
authoring `devbox.json` on either side is safe; the remaining discipline is
ordinary git — the VM commits `devbox.lock`, pull it before pushing again.

Resolution needs network on the first add. `.devbox/` is per-machine and
gitignored. direnv trusts every `.envrc` under the guest home through
`/etc/direnv/direnv.toml`; the NixOS module exports `DIRENV_CONFIG=/etc/direnv`,
so a `~/.config/direnv/direnv.toml` is silently ignored — never put the
whitelist there.

That `git add`-before-Nix discipline still applies to yolobox's own repo,
but only on a local clone someone is hacking on directly — the VM itself
no longer holds a checkout to run `nixos-rebuild` against. It builds from
a published flake ref instead (see "Distribution: the flake ref replaces
the guest checkout" below), so a file the VM has never seen is simply a
file the flake ref does not contain, not an untracked file Nix skips over.

`yo bootstrap` decides whether the VM needs a reboot by comparing `readlink
/nix/var/nix/profiles/system` against `readlink /run/current-system` — the
same two readlinks `yo gc` compares to decide whether it may run
`nix-collect-garbage -d`. Equal means the built generation is already
running, so bootstrap skips the reboot; different means `nixos-rebuild
boot` just installed a generation the box has not started yet, so it
restarts the VM and re-checks, aborting with a `df /boot` hint if the two
still disagree (the half-failed bootloader install from the ESP section
below).

A project is any git repo at any depth under the guest home, not only
`<group>/<repo>` — a monorepo puts one at depth 7. So the walk —
`yolobox-guest projects`, reached from `pick_project` in `yo` — is bounded
by pruning `BUILD_DIRS` (`node_modules`, `target`, `.next`) and every
hidden directory (`.git` itself included), not by a depth cap, because a
depth cap has to be re-guessed every time a monorepo nests one level
deeper. That pruning is real today; it was not always, and the gap between
the two is worth recognising on its own. `a1c8c9b` ("fix: remove maxdepth
from fuzzy project search") deleted the `-maxdepth 5` the walk used to
carry — a regression, because nothing replaced the depth bound it was also
accidentally providing — but the defect it exposed was already there
before that commit, just masked. `-mindepth` is a *global* find option: it
suppresses evaluation of the whole expression — `-prune` is a test, so it
is suppressed too — for every entry above the given depth. The walk ran at
`-mindepth 2`, so every top-level dotdir, `~/.local` included, was never
tested against `-name '.*'` and never pruned; find walked straight into
whatever it held, which in the incident that surfaced this was podman's
0700 volume storage. GNU find exits 1 on any unreadable directory, and the
walk's exit code used to be checked unconditionally, so the whole command
aborted and the project list already gathered was thrown away with it.
Recognise that shape: `yo enter <fuzzy>` used to die with a find
`Permission denied` and no project list at all, even though the walk had
already found real projects before it ever reached the unreadable one.
`-maxdepth 5` had hidden the `-mindepth` hole for as long as the podman
volume sat below depth 5, which is exactly what made `a1c8c9b` look like a
clean fix and not a regression at the time. The walk is now `-mindepth 1`,
prunes every top-level dotdir explicitly rather than leaning on a maxdepth
to keep it out of reach, and an unreadable directory now warns — via `yo`
printing find's own stderr — and continues with the partial list rather
than discarding it; only an empty list still refuses with "no projects in
the VM".

Pruning `BUILD_DIRS` is new behaviour, and it opens a failure shape that
looks exactly like the old maxdepth-cap symptom: a repo literally named
`target`, `node_modules` or `.next` is now invisible to the picker, and
fzf says "no project matches" for a repo that plainly exists — the same
wording the maxdepth cap used to produce for an unrelated reason. Do not
assume "no project matches" means the cap is back; check first whether the
repo's own directory name collides with `BUILD_DIRS`.

## The guest helper: yo's shell moved into the box

`nix/guest/yolobox-guest.sh`, built by `nix/guest.nix` into the
`yolobox-guest` package and put on both accounts' `PATH`, is where `yo`'s
guest-side logic lives now: the project walk, the gitconfig-root walk, the
landing-dir ancestor walk, `ensure-repo`, `generations`, both `gc` tiers,
and the AWS probe. Before this it was ~200 lines of bash trapped inside
Python string constants (`PROJECT_FIND`, `GC_MACHINE`, and the rest), and
nothing checked any of it — no shellcheck, no `bash -n`, no test that ever
exercised a single find expression. `writeShellApplication` buys three
things a Python string literal never could: a build-time shellcheck pass,
so a bad find expression is now a *build* error rather than a runtime one
(this file's own "fail at the earliest stage" rule, finally reaching this
bash); `set -euo pipefail`; and a pinned `runtimeInputs` PATH. `yo` reaches
every subcommand the same way, `ssh ... yolobox-guest <sub> [args]`, one
account per subcommand: `projects`, `home-roots`, `landing-dir`,
`ensure-repo`, `gc-user` and `aws-check` run as the agent; `generations`
and `gc-machine` run as the operator, because ext4's root reserve — the
thing that makes any of this recoverable on a genuinely full disk — is the
operator's alone.

`BUILD_DIRS` (`node_modules`, `target`, `.next`) is the single place
build-output directory names are spelled, inside the guest script. The
project walk prunes them, because a repo living inside `node_modules` is
not a project a person would pick; `gc --deep` matches them, because there
they are the thing being deleted. One list serves both on purpose — before
the move there were two, `PROJECT_FIND` and `GC_USER`'s `project_targets`,
and neither knew the other existed.

The Mac side never guesses what a box supports; it reads the exit code.
A subcommand `yo` was built to call after `yolobox-guest` shipped it exits
**64**, with `yolobox-guest: unknown subcommand '<sub>'` on stderr — the
box is older than this Mac's `yo`. A box with no `yolobox-guest` at all
exits **127**, with the shell's own "command not found" on stderr — it
predates the helper entirely. Both route through `guest_helper()` and
`helper_skew()` in `yo` into the same message: which version this Mac's
`yo` is, which version built the box, and `yo bootstrap` as the fix. The
exit code alone is never enough in either direction, because a
subcommand's own child process — `podman`, `npm` — can legitimately exit
127 or 64 for reasons that have nothing to do with the helper; both
branches require the stderr marker too before reinterpreting the exit,
and the corollary is load-bearing: no subcommand's own stderr may ever
contain the literal string `unknown subcommand`. It follows that adding a
subcommand to the guest script is not enough on its own to make it
callable — the box has to be rebuilt (`yo bootstrap`, or a plain
`nixos-rebuild switch` for someone iterating inside the VM) before that
subcommand exists on the box's `PATH` at all; calling it against an
un-rebuilt box hits exactly the 64 path above, correctly, because from the
box's point of view the subcommand really is unknown.

## `yo`'s own CLI: one parser, not two lists and 13 copies of a check

`build_parser()` is the single declaration of `yo`'s command line:
`add_subparsers` plus `set_defaults(func=cmd_x)` per subcommand, and
`main()` collapses to parse, run the `limactl` preflight, then
`args.func(args)` — every command function takes the parsed
`argparse.Namespace` now, not a raw `Sequence[str]`. Before this there were
two lists nothing kept in agreement, a hand-written `USAGE` string and a
`COMMANDS` dict, plus 13 separate copies of the same arity check scattered
through the command bodies. That duplication had already drifted: `up`,
`down` and `status` silently accepted and ignored extra arguments while
the other nine zero-argument commands rejected them, because someone
copied the check to nine bodies and not the other three. `yo up bogus`
used to succeed; it now fails, like every other command with no positional
arguments. The same class of fix reaches `disk-grow`: its bespoke
`.isdigit()` check used to reject `disk-grow -1` with "must be a whole
number of GiB" before ever reaching the "greater than 0" check; argparse's
`type=` callable reaches `int()` first, so a negative value now reports
"must be greater than 0 GiB" instead — a wording change, not a behaviour
change, worth recognising rather than mistaking for a new bug.

`yo ssh`'s trailing arguments are declared `nargs=argparse.REMAINDER`, and
that choice is load-bearing rather than a default reached for out of
laziness: `rebuild_hint()` prints
`yo ssh sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake '…'`
as the documented remedy for a stale generation (see "Distribution" below),
so if argparse ever parsed `--impure` as its own flag instead of passing it
through, the tool's own printed advice would stop working. Verified against
real argparse: every invocation whose first token is not option-like
passes through untouched, `yo ssh sudo … --impure --flake …` included. The
one shape that changes is a leading-dash *first* token — `yo ssh -v`,
`yo ssh --help` — which now exits 2 from argparse itself rather than
reaching the VM. That is not a regression: both forms were already broken,
because REMAINDER (like the old hand-rolled passthrough) only ever hands
the tokens to ssh as a *remote command* after the hostname, so `--help`
used to fail inside the VM as `bash: --help: command not found`. `yo ssh
-h` is the one case that actually improves: it now prints `yo`'s own ssh
help instead of failing the same way.

`allow_abbrev` is on by default and nothing in `build_parser()` turns it
off, so `yo gc --de` and `yo gc --y` now parse as `--deep` and `--yes`
where the old hand-rolled flag loop rejected any prefix outright. Worth
carrying in your head for a command whose job is deleting things.

`YO_VERSION = "dev"` (line ~32) must survive as that exact literal
assignment, unreformatted. `nix/pkgs/yolobox.nix` overwrites it with
`--replace-fail` and `homebrew/yolobox.rb` with `inreplace`, both matching
the literal text; either patch is how a release build gets its real
version number baked in. Reformat that line — reflow it, requote it,
anything that changes its exact text — and the Nix build fails loudly at
`--replace-fail` (the safer failure) while the brew formula's `inreplace`
can silently no-op and ship a binary that still reports `dev`.

`enter`, `code` and `zed` take `project` as `nargs="?"`, so `yo enter ""`
and `yo enter` produce different values — `project=''` against
`project=None` — and are different invocations: an empty fzf query against
mirroring the guest twin of the current directory. `target_dir` branches
on `project is not None`, never on truthiness, precisely so the empty
string does not collapse into the no-argument case.

## Distribution: the flake ref replaces the guest checkout

Earlier, `yo bootstrap` pushed this repo into the VM over git
(`guest_repo_ensure`, a `yolobox` git remote, `git push`) and then ran
`nixos-rebuild --flake '<guest path>#yolobox'` against that pushed-in
checkout. Now `yo` itself is a distributed package —
`brew install aka-rider/tap/yolobox` (tap `aka-rider/tap`) or `nix run
github:aka-rider/yolobox` — and `yo bootstrap` builds the VM directly from
a pinned flake ref, `nixos-rebuild boot --impure --flake
'github:aka-rider/yolobox/v<version>#yolobox'`, with no push and no guest
checkout at all. The version is baked into `yo` at package build time;
the release workflow takes the published release's tag
(`github.event.release.tag_name`) as the single source of truth: it writes
`.version` from that tag, commits the stamp to `main`, and force-moves the
tag onto the stamp commit. The moving tag is the part worth understanding.
Both channels read `.version`, but at different moments — homebrew reads it
out of the `git archive` tarball CI builds, there and then, while nix reads
it out of whatever tree the published tag points at, whenever a guest later
fetches the flake ref. So writing the stamp into the tarball alone would
leave every nix fetch on a stale version; the stamp has to be inside the
tagged tree, and moving the tag is how it gets there. What makes that safe
is a guard: the workflow refuses unless the release tag is already the tip
of `main`, because force-moving a tag cut from an older commit would
republish different content under a name someone may already have fetched.
The formula itself lives here too, at `homebrew/yolobox.rb`: the release job
renders it into the tap from that same tarball, hashed locally rather than
re-downloaded, so the tap never holds a hand-edited formula and the hash
never rests on GitHub's non-stable auto-tarballs. `YOLOBOX_FLAKE` overrides
the ref for anyone running their own fork wholesale, e.g.
`YOLOBOX_FLAKE=github:you/yolobox/your-branch yo bootstrap`. Customising
the box now means dropping a `/etc/yolobox/local.nix` inside the VM, which
`nix/base.nix` imports when present, then `sudo nixos-rebuild switch
--impure --flake '<same ref>#yolobox'` — no clone, no push, nothing for
Nix to have "never seen".

This shape introduces failures with no analogue in the old push-based one,
each recognisable on its own:

- **The repo goes private.** `nixos-rebuild` fetches the flake ref over an
  anonymous `git+https`/`github:` fetch with no credentials configured, so
  a private `aka-rider/yolobox` makes every `yo bootstrap` (and every
  customisation rebuild) fail with a fetch/auth error. The repo staying
  public is now load-bearing for every box in the field, not just a
  publishing preference.
- **`YO_VERSION` is still `dev`.** Running `./yo` straight out of an
  unreleased clone has no version to pin a flake ref from — the flake_ref
  function refuses outright rather than guess at a ref that may not exist yet, and
  says so on stderr, naming `YOLOBOX_FLAKE` as the way out. Recognise this
  by the exact wording of that refusal, not by a bare Nix fetch error.
- **The box and the Mac's `yo` come from different releases.** The guest
  no longer ships `yo` at all — it used to, only so `yo --version` could
  say which release built the box, and every other subcommand needs
  `limactl` and `~/.lima`, neither of which exists in the guest. Instead
  `nix/base.nix` writes the release into `/etc/yolobox/version`, and `yo
  status` prints both versions and warns on stderr when they differ,
  naming `yo bootstrap` as the fix. A box built before that file existed
  reports `unknown`. `yo`'s `main()` still carries a `limactl` preflight
  guard, and `--help`/`--version` still never reach it — but the exemption
  is no longer two hand-written early returns remembering to skip it.
  `build_parser()` declares `--version` as `action="version"` and argparse
  raises `SystemExit` for both `--help` and `--version` inside
  `parse_args()`, before `main()` ever gets to the
  `require_tool("limactl", ...)` line below it, so the exemption holds by
  construction rather than by two branches someone could forget to update
  together. `nix run github:aka-rider/yolobox` inside a guest still fails
  loudly on that preflight, never with a bare `command not found`.
- **`nix flake check` throws with no explanation.** `flake.nix` reads
  `YOLOBOX_USERNAME` from the environment and `throw`s when it is unset,
  because it has no pure way to learn the host username otherwise (see
  "Two accounts: the operator mirrors the host, the agent does not" above). A bare `nix flake check`
  hits that throw immediately; it needs `--impure` with
  `YOLOBOX_USERNAME=$(id -un)` set, same as every `nixos-rebuild` in this
  repo.
- **Release published from a non-main commit.** The release workflow refuses
  to run unless the release tag is already the tip of `main`, because the
  workflow force-moves the tag onto its stamp commit — moving a tag from an
  older commit would silently republish different content. Recognise this by
  a workflow failure on a release created from a feature branch, with an
  explicit refusal message before any tag move or archive happens.
- **`bump-tap` fails on the tag `release` just moved.** The v0.9.0 release
  (2026-09-02) showed it: `verify` and `release` both succeeded, then
  `bump-tap`'s bare `actions/checkout@v6` died with `The ref
  'refs/tags/v0.9.0' does not point to the expected commit '4069fe0…'. The
  ref may have been updated after the workflow was triggered.` With no
  `ref:`, checkout resolves `github.ref` (the release tag) and then asserts
  that tag still points at `github.sha` — where it pointed when the release
  was published (`testRef` in `src/ref-helper.ts`). Two jobs earlier,
  `release` had force-moved the tag onto its own stamp commit, so the
  assertion fails by construction. This is a behaviour change in
  actions/checkout v6.0.2 (`actions/checkout#2356`, "Fix tag handling"): a
  tag used to be fetched by sha, so a moved tag went unnoticed; now it is
  fetched by name and compared, and no input turns the comparison off. The
  check is skipped only when `ref:` names an explicit branch or tag, because
  `commit` is then never assigned — which is why `release`'s own `ref: main`
  checkout was never at risk, and why `bump-tap` now pins
  `ref: ${{ github.event.release.tag_name }}`, the moved tag, whose tree is
  exactly what the tarball was built from. Two rules follow from the same
  mechanism: after a release failure, re-run only the failed jobs, never the
  whole run, because `verify.yml` (shared with `ci.yml`, so its checkout
  stays bare on purpose) would replay against the already-moved tag; and a
  re-run always executes the workflow file from the triggering commit, so a
  fix to the workflow itself can only be proven by cutting a new release —
  v0.9.0 shipped with a tarball and no tap formula, and v0.9.1 was the
  first release to carry both.

## tmpfiles rules apply on `switch`

The VM carries `systemd-tmpfiles-resetup.service`, the switch-time twin of
the boot-only setup unit, so a new tmpfiles rule takes effect on `switch`
with no reboot. It re-runs every rule, including force-replacing `L+`
links, which `nix/harnesses.nix` relies on to hold `~/.local/bin/claude`
and `~/.local/bin/opencode` at the box's own paths across the one window
the launcher path unit cannot see on its own — an install made while no
user manager of the agent's was running.

## The ESP is 249 MiB and holds one kernel

`/boot` is the EFI partition itself, `/dev/vda1`, 249 MiB (260,796,416
bytes), mounted there to match the shipped nixos-lima image so a fresh
instance never needs a mount migration. The size is baked into the prebuilt
image and cannot be grown: vda1 ends at sector 526335 and vda2 starts at
526336, the very next sector, with no gap to grow into.

A kernel set is about 92 MiB and `linuxPackages_latest` brings a new one
every few weeks. Two sets fit; three never do — and the count that matters
is not the retained one. `install-grub.pl` copies every retained
generation's kernel into `/boot/kernels` first and unlinks obsolete files
only after the `grub.cfg` rename. So peak usage during a switch is the
retained sets plus every set already there. With `configurationLimit = 2`
the next kernel bump holds three sets transiently and dies mid-copy; with
`1` the peak is about 198 MB against 260.8 MB. Lowering the limit frees
nothing on the switch that applies it, because pruning runs last; one more
switch without a kernel change drains the old set — verified here, taking
`/boot` from 198,569,984 bytes used down to 105,672,704.

The failure surfaces late and lies. The closure builds, the profile
advances, and only then does the bootloader install die with `No space left
on device`. The box keeps running the old generation while
`/nix/var/nix/profiles/system` claims the new one — `readlink
/run/current-system` and the profile disagree, and a reboot comes back on
the old one. If the switch was piped through `tail`, the shell reports exit
0. Check `df /boot` first when a switch behaves strangely.

The trade: no rollback generation survives a kernel bump. Acceptable,
because the VM is rebuildable from this repo.

Moving `/boot` onto the root filesystem was tried (`a2057d8`, reverted in
`03e2dbc`). It gives unlimited space, but makes `nix/base.nix` diverge from
the image, so every fresh instance would need a manual mount migration
before its first switch.

## A full disk: recognising it without getting stuck

`/dev/vda2` is the only real filesystem, and `/tmp` is a directory on it,
not a tmpfs. So a full disk first shows up as the agent harness dying with
`ENOSPC ... mkdir '/tmp/claude-501/...'` before any command runs.

Two facts make it survivable. ext4 reserves 1,057,641 blocks x 4 KiB = 4.3
GB for root that `df`'s "Avail" column does not count, so the operator's
`sudo`, over `yo ssh`, still works while every unprivileged command dies —
the agent has no `sudo` at all, so an agent session dying is not evidence
either way about how full the disk actually is. And `yo ssh` reaches in
from the Mac, which the full disk cannot hurt — hence the remedy, `yo gc`,
lives on the Mac.

In the one incident so far, the `rune` project's `target/` under the
guest home held 41 GiB, 43% of the disk: `rune/Cargo.toml` had no
`[profile.dev]`, so every dev build and
every integration test linked its own unstripped ~230 MiB binary, and cargo
never prunes old hashes. Fixing `Cargo.toml` per repo would leave the next
Rust project exposed, so `nix/base.nix` exports
`CARGO_PROFILE_DEV_DEBUG=line-tables-only` box-wide: cargo ranks the
environment above both `Cargo.toml` and `.cargo/config.toml`, and the
`test` profile inherits `dev`, so every dev build and test binary in the
box keeps line tables and drops the rest of the DWARF, whatever the repo
declares. Backtraces still carry file and line; stepping through variables
in a debugger does not.
Runners-up: 32 GiB of dead nix store paths, 4.7 GiB podman, 2.9 GiB
`/root/.cache/nix`, 2.5 GiB `~/.npm`.

The podman share had a second cause: `virtualisation.podman.autoPrune`
renders a *system* `podman-prune.timer` that runs as root, and this box is
rootless-only, so root owned nothing and the weekly prune no-op'd silently
for as long as it existed. `nix/podman.nix` now declares a *user* timer
instead, `podman-prune.timer` under `systemd.user`, gated with
`ConditionUser=agent` so the operator's user manager skips it, running
`podman system prune -f` as the account that owns the storage. It fires
weekly with `Persistent=true`, so a week missed because no agent session
was open runs at the next login. `systemctl --user list-timers` as the
agent must show it; `systemctl list-timers` as the operator must not.

`yo gc` reports, and `--yes` now runs two ordered ssh sessions, one per
account, not one session doing everything. The machine tier runs as the
**operator** — journald, `nix-collect-garbage`, root's nix cache — and it
runs first on purpose: ext4's root reserve is what makes the rest of the
cleanup possible at all on a genuinely full disk. The user tier then runs
as the **agent** — npm cache, rootless podman, and under `--deep --yes`
the project tier: `target`, `node_modules` and `.next` deleted anywhere
under the agent's home, hidden directories pruned. The split is not
cosmetic: npm's cache, podman's storage and every project checkout live
under the agent's home now, not the operator's, so a `yo gc` that only ran
as the operator would measure and clean an empty home and still report
success — a silent no-op that never touches the account actually holding
the state. `.venv` and `.devbox/virtenv` are excluded — `nix/lsp.nix`
points basedpyright at `.venv`, and `virtenv` is devbox's toolchain, not
build output.

Two traps. Deleting a build directory the project's `.gitignore` does not
match dirties the checkout, and every later `git push yolobox` is refused
with no obvious link back; `iurii.net` anchors `/node_modules`, so a nested
`web/node_modules` is tracked. Hence `--deep` asks `git check-ignore` per
directory. And `nix-collect-garbage -d` deletes the running generation
when a switch half-failed (see the ESP section); `yo gc` compares the two
readlinks and reports a skip, which means the box needs a repaired switch
before it needs a garbage collection.

## Memory pressure: swap first, then the right victim

The VM once had no swap, so memory pressure went straight to the kernel's
global OOM killer with no warning stage. On 2026-08-26 a `nixos-rebuild
switch` on a box already at ~2.1 GiB available of 12 GiB (ten agent
sessions, a five-container podman stack, a Rust link job) tipped it over.
The journal shows the kernel killer, not `systemd-oomd`, picking
`.chrome-wrapped` and gunicorn workers first, and eventually the uid-501
user manager: `user@501.service` failed with result `signal` and every
rootless container died with exit 137. The agents themselves survived,
because ssh session scopes hang off `user.slice` directly, not under
`user@.service` — the blast radius of a user-manager kill is exactly the
rootless containers.

Two changes in `nix/base.nix` address the two halves. `swapDevices`
declares a 16 GiB `/var/swapfile`, so reclaim has somewhere to go before
the killer runs, and `lima/yolobox.yaml` raised the box to 16 GiB and 8
cpus. And systemd's upstream `user@.service` ships `OOMScoreAdjust=100`
(verified with `systemctl show user@1000.service -p OOMScoreAdjust`),
which makes the per-user manager a *preferred* victim over every system
service at 0. A drop-in (`overrideStrategy = "asDropin"`, so upstream's
unit text is kept, not replaced) sets it to -500. Podman sets its own
`oom_score_adj` on containers (200 in the incident), so they stay
individually killable; what the drop-in changes is that the kernel takes
one container worker, or the agent's own link job, before it takes the
manager that holds all of them.

## The root disk grows; the ESP cannot

`~/.lima/yolobox/disk` is sparse: it reads 100G and allocates only what
the guest wrote. The guest mounts `/` with `discard` and `fstrim.timer`
runs weekly, so freed blocks are punched back out of the host file.
Over-declaring the ceiling costs nothing, which is the intended use.

Raising the ceiling has two halves, and they do two different things.
`fileSystems."/".autoResize` was already set; it renders `x-systemd.growfs`
into `/etc/fstab`, which grows the ext4 *filesystem* to fill whatever
partition it already sits in — that half was never the gap. The host half
is `limactl edit yolobox --disk <GiB>`, grow-only, instance stopped. The
guest half is `boot.growPartition = true` in `nix/base.nix`: a `growpart`
unit that runs before `systemd-growfs-root.service` and grows the
*partition*, vda2, to fill the disk. Its `SuccessExitStatus = "0 1"` makes
an already-grown boot a harmless no-op, so re-running it on every later boot
costs nothing. This partition-growing half was missing, because vda2 only
reached 99.7G thanks to nixos-lima's own image config growing it at first
boot, before our config took over.

Skipping the guest half is silent: the backing file grows, the guest boots
clean, `df` is unchanged, and nothing logs. `systemctl show -p LoadState
--value growpart` must answer `loaded`; `not-found` means the running
generation cannot grow anything. `growpart` only runs on a boot whose
generation carries the option, so the switch must land before the resize —
`yo disk-grow` checks this before touching anything.

## Browsers and the virtual display

The display is still `:0`, 1920x1080, Xvfb with openbox on it, and
`DISPLAY=:0` is still set box-wide — but nothing in this repo wraps a
browser any more. Two vendor tools reach it instead: claude's official
`playwright` plugin (`npx @playwright/mcp@latest`) and, for pi,
`pi-agent-browser-native` over Vercel's `agent-browser` CLI. Playwright
runs headed on the display because `DISPLAY` is set; agent-browser is
headless unless told `--headed`.

The one thing the box must supply is the browser binary itself, because
Playwright's own browser downloads do not run on NixOS. So
`PLAYWRIGHT_MCP_EXECUTABLE_PATH` points at nixpkgs' `chromium` (151, cached
for aarch64); agent-browser finds the same chromium on PATH and through
the extension's own config file (see "MCP" below), and
`AGENT_BROWSER_EXECUTABLE_PATH` is deliberately not set, because
pi-agent-browser-native disables its managed session restore whenever that
variable is present. `agent-browser install` must never be run: it
downloads a glibc Chrome for Testing that cannot execute here, and having
run it leaves a binary that looks installed and is not.
`PLAYWRIGHT_MCP_CAPS=vision,pdf,devtools` turns on the three capabilities
that are off by default.

`PLAYWRIGHT_MCP_ISOLATED=1` is set box-wide, and isolated mode and a
user-data-dir are mutually exclusive — the server throws when both are
set. That is a deliberate choice: run every Playwright launch isolated,
with no persistent profile at all, rather than maintain one, so no browser
state — cookies, logins, anything — survives past the launch that created
it. `PLAYWRIGHT_MCP_USER_DATA_DIR` must never be set alongside it.

What the box's claude launcher (see "The harnesses come from their
vendors; the box owns `~/.local/bin/claude`" below) does compute per
project is the *output* directory, not a profile. Before `exec`, it
derives the project from the logical cwd — `git rev-parse --show-cdup`
against `$PWD`, never git's physical toplevel, because `~/wrk` is a
symlink to `~/Developer` and the mirror rule (see "The mirror" above)
spells paths logically — relative to `$HOME`, slashes turned into `--`,
and exports `PLAYWRIGHT_MCP_OUTPUT_DIR=$HOME/artifacts/<project>`,
creating the directory first; a cwd at or outside `$HOME` leaves the
variable at its box default instead. Snapshots and unnamed screenshots
land there because
Playwright honours that variable directly. A screenshot given an explicit
`filename` does not: Playwright resolves an explicit name against the MCP
workspace root — the first root the client advertised, else the server's
own cwd — by upstream design (microsoft/playwright#42487, #42494), and
Claude Code has advertised its own launch directory as MCP root #1 since
2.1.203. Left alone, a named screenshot would therefore land inside
whatever project checkout the session started in, exactly what keeping
output under `~/artifacts` exists to prevent. `nix/display.nix`'s
`PreToolUse` hook, `yolobox-playwright-artifacts`, catches this before
the tool runs: it rewrites `filename` in the tool input, keeping a
relative name's subpath under the output dir and reducing an absolute
name to its basename there.

The old guard that refused loudly when the X socket was missing is gone
with the per-engine wrappers that used to carry it; upstream's silent
fall back to headless is what happens now.

Things learned the hard way, each one line:

- `openbox` waits for the X socket in `ExecStartPre`; a `Restart=` loop
  trips systemd's start limit and stays failed.
- Use `${pkgs.xvfb}/bin/Xvfb`; `lib.getExe` on it names a binary that does
  not exist.
- No tmpfiles rule for `/tmp/.X11-unix`; systemd ships one.
- The screen-record pidfile lives in `~/.local/state/yolobox`, because
  `/run/user/$UID` dies at logout while the backgrounded `ffmpeg` survives.
  `SIGINT` finalizes the mp4; `SIGKILL` corrupts it.
- Headed screenshots without `fonts.packages` render as tofu.
- Default ffmpeg has no x11grab; `nix/display.nix` uses `ffmpeg-full`.
- Two sessions share one browser: Chromium's singleton lock forwards the
  second launch (~8 s). Not isolation, not a failure.
- Chromium's storage flushes lazily; a session killed without
  `browser_close` can lose its last write.

## herdr: the VM runs its own server, panes are real ptys

The Mac used to run the only herdr server, and `yo enter` forwarded that
server's unix socket into the guest over `ssh -R`, to
`/run/yolobox/herd-host.<pane>.sock`, setting `YOLOBOX_HERD` /
`HERDR_PANE_ID` / `HERDR_SOCKET_PATH` for guest-side hooks to report
through. That existed for one reason: a host pane's foreground process
was `ssh`, not the agent, so herdr had nothing of its own to detect.

The VM now runs its own herdr server as the `agent` account —
`nix/herd-server.nix`, a `systemd.user` service wanted by
`default.target` and gated `ConditionUser = agent` — up from boot on the
account's existing lingering (see "The harnesses come from their
vendors" below for why lingering matters there too). Panes opened
against that server are real ptys, so herdr detects an agent inside them
natively, the same way it detects one on the Mac.

Agents run in herdr's own **machine panes**, not in `yo enter`. `yo
enter` stays a plain landing shell — `ssh -t … exec $SHELL -l`, nothing
herdr-aware about it at all — and a session started there is invisible to
herdr, the same as `yo ssh`, `yo code`, `yo zed` and every t3-spawned
agent: none of them opens a pty on the guest herdr server itself, so all
of them show as `unknown` or nothing at all. The place to run an agent is
a workspace opened on the saved `yolobox` machine (below), because that
pane really is a pty on the VM's own server. `cmd_enter` knows this and
says so: when `HERDR_ENV=1` — meaning the command itself is running
inside a herdr pane already — it prints on stderr that an agent started
here is invisible to herdr, naming the `yolobox` machine as where to run
agents instead. Inside the guest itself, navigation is left entirely to
the agent's own dotfiles (`z`, `fzf`) rather than to any `yo`-shaped
wrapper — nothing named `yo` exists in the guest at all.

**The regression this design fixes, and how to recognise it if it comes
back.** `d3fe9ff` ("herdr v0.9 supports cross-machine connections
natively") removed `yo enter`'s old forwarding of the Mac pane's herdr
socket, on the theory that native machine panes already made it
unnecessary — but nothing yet moved agents into those panes, so the
removal shipped with no replacement. The shape it produced: `herdr
machine list` on the Mac came back empty, because nothing had ever run
`herdr machine add` to adopt the VM's server; inside the VM, `herdr pane
list` was empty too even with `pi` plainly running, because its
foreground process sat directly under `sshd` — `yo enter`'s own session —
never under a pty the guest herdr server had opened, and carried none of
`HERDR_ENV`/`HERDR_PANE_ID`/`HERDR_SOCKET_PATH`, the env a real herdr pane
gets for free. From the Mac side an agent running exactly this way just
looked permanently idle, with no error anywhere to point at the cause.

The Mac attaches to the guest server as a saved SSH **machine**, adopted
rather than started: `herdr machine add <alias> --label yolobox` checks
the running server's capabilities, never its process ancestry, so the
systemd unit and `machine add` never fight over who owns the process.
`yo bootstrap` does this for you now — `ensure_herdr_machine()`, run at
its end, checks `herdr machine list --json` for an entry whose SSH target
is `yolobox` and, finding none, runs `herdr machine add yolobox --label
yolobox` itself, raising on a non-zero exit with herdr's own stderr.
Adoption is also why keeping `herdrPkg` in `environment.systemPackages`
(`nix/harnesses.nix`) is load-bearing rather than incidental: `machine
add` installs its own binary into `~/.local/bin/herdr` only when no
compatible binary turns up on its probe list, and that list includes
`/run/current-system/sw/bin/herdr` — drop the package from
`systemPackages` and a fresh `machine add` would try to install its own
binary instead of adopting the system one, and a non-interactive install
is refused outright rather than done silently.

Compatibility is gated by capability now, not by the version-equality
rule this file used to carry. The guest reports `version 0.9.0, protocol
22, endpoint_protocol_generation: 1, surface_interest: true, health_check:
true, detached_server_daemon: true`; herdr's `remote_server_restart_reason`
gates a saved machine on four of those — `endpoint_protocol_generation`,
`surface_interest`, `health_check`, `detached_server_daemon` — and
`endpoint_protocol_generation` has to equal the client's own constant
exactly. It is coarser than the version string, so the VM's herdr and the
Mac's herdr may run different versions as long as the generation agrees;
the old rule, exact version equality between the two, was strictly finer
than what compatibility actually needs.

The VM's herdr is pinned regardless, through the existing
`yolobox.harness.herdr.version`/`.hash` valve in `flake.nix` — to 0.9.1,
because the pinned nixpkgs still carries 0.8.2, and taking 0.9.1 from
nixpkgs would have dragged a whole nixpkgs move, a new kernel included,
onto a 249 MiB ESP that already holds exactly one kernel set (see "The
ESP is 249 MiB" below). The pin fetches the published
`herdr-linux-aarch64` release asset directly (`nix/pkgs/herdr-bin.nix`);
it carries no `PT_INTERP` and no `PT_DYNAMIC` — statically linked — so it
runs on NixOS unpatched, with none of the nix-ld dance a dynamically
linked upstream binary would otherwise need.

Two failure shapes are worth recognising, because neither one produces an
error anywhere.

**`herdr agent list` comes back empty for a session that is plainly
running claude.** herdr classifies a pane's agent by the name of its
foreground process, and the box's claude launcher `exec`s
`~/.local/share/claude/versions/<version>` directly, so the process herdr
actually sees is named after a version directory (`2.1.268`, say), never
`claude` — classification never reaches herdr's screen manifest at all.
The manifest itself is fine throughout, which is the proof the gap is in
classification and not detection: `herdr agent explain --file <pane
dump> --agent claude` matches rule `live_prompt_box` and returns `idle`
even while `herdr agent list` shows nothing. The fix is one line in the
launcher ahead of its `exec`: `export HERDR_AGENT=claude` — herdr reads
the hint from the process environment rather than the process name.
herdr's own docs warn that `HERDR_AGENT` "cannot be seen if you set it
only inside a VM"; that warning describes a different topology, a
host-side herdr watching into a VM from outside, and does not apply here,
because the server reading the hint now runs inside the VM with the
process rather than watching it from outside. With the hint in place, all
three states were observed live through `herdr agent get`: `idle` (rule
`live_prompt_box`), `working` (during a real turn), and `blocked` (rule
`bash_permission_prompt`, held steady while a real permission dialog was
up — plan mode intercepts a destructive command before the permission UI
ever appears, so re-testing `blocked` needs the pane in manual mode
first).

**A saved machine sits stuck rather than connecting.** That is
`remote_server_restart_reason` refusing one of the four capabilities
above, almost always `endpoint_protocol_generation`, because that is the
one that actually gates compatibility — a Mac herdr and a VM herdr that
both look like "0.9.x" in `herdr status` can still disagree on
generation, so read the generation, not the version string sitting next
to it. The remedy is the same escape hatch as the pin itself: bump
`yolobox.harness.herdr.version`/`.hash` to whatever generation the Mac's
herdr now speaks.

A third shape is less a herdr bug to diagnose than a trap to avoid
recreating. pi's own herdr integration writes
`~/.pi/agent/extensions/herdr-agent-state.ts` (`herdr integration install
pi`, run by `yolobox-harness-install`) — a vendor-owned file, not one the
flake renders. An earlier design needed `yolobox:pi` to be pi's one and
only source into a Mac-side herd, so a tmpfiles `r` rule deleted that
exact path on every boot and switch; that need is gone, and so is the
rule. Never re-add a tmpfiles rule deleting a herdr-installed integration
file: `systemd-tmpfiles-resetup.service` (see "tmpfiles rules apply on
switch" below) reapplies `r` rules on every switch, not just once, so the
rule would silently un-install the integration on the very next switch —
and the symptom, pi missing from the herd with no error anywhere, would
look exactly like the integration never having installed at all.

`yolobox-harness-install` used to guard the install with
`[ -f ~/.pi/agent/extensions/herdr-agent-state.ts ]`, so once the file
existed it was never touched again — the herdr 0.9.0 → 0.9.1 bump moved
the integration from v8 to v9, and a box installed before that bump kept
running v8 forever, silently, with `herdr integration status` reporting
`pi: outdated` the whole time and nothing reading that output. The guard
is now `herdr integration status | grep -q '^pi: current' ||
herdr integration install pi`, so a version bump is picked up on the next
run of the install service rather than needing the extensions file
deleted by hand.

`herdr machine add yolobox --label yolobox` (`yo bootstrap`'s
`ensure_herdr_machine`) can fail loudly with "No ED25519 host key is known
for [127.0.0.1]:60022 and you have requested strict checking. Host key
verification failed." herdr runs system ssh through its own generated
config, which `Include`s `~/.ssh/config`, which `Include`s
`~/.lima/yolobox/ssh.config` where yo's `Host yolobox` alias lives — but
for every saved machine herdr also appends `-o StrictHostKeyChecking=yes`
on the ssh command line itself
(`apply_noninteractive_ssh_options` in herdr's `src/remote/attach.rs`),
and a command-line option always outranks the same keyword set inside a
`-F` config file. So the alias cannot fix this by relaxing checking: it
used to carry `StrictHostKeyChecking no` paired with `UserKnownHostsFile
/dev/null`, and herdr's own `yes` silently overrode the `no` while the
`/dev/null` guaranteed no key could ever be known — strict checking could
never pass, by construction. The alias now pins the real key instead:
`UserKnownHostsFile` points at a yo-owned file,
`~/.lima/yolobox/known_hosts`, which `ensure_guest_known_hosts` populates
from the guest's own `/etc/ssh/ssh_host_*_key.pub`, fetched over the
operator's already-trusted lima connection — never a channel that itself
needs the key it is trying to learn. It runs from `vm_up` right after
`limactl start` and again from `cmd_bootstrap` just before
`ensure_herdr_machine`, since a recreated VM has new host keys.

## The harnesses come from their vendors; the box owns `~/.local/bin/claude`

claude, pi, opencode and agent-browser are not nixpkgs packages any more.
They install into the agent's home from their own vendors and self-update
there, and the box gives up on owning their versions. What the box owns
instead is one path: `~/.local/bin/claude`. That inversion is the whole
design, and the reason for it is an outage this file used to describe from
the other side.

Claude's settings reach it only as `--settings <file>`: Claude Code
assembles `policySettings` from three tiers — remote, MDM,
`/etc/claude-code/managed-settings.json` — and takes only the first
non-empty one, no merge, and this account's `~/.claude/remote-settings.json`
holds `{"channelsEnabled": true}`, which is always non-empty, so the
`/etc` tier is discarded whole with nothing logged. `--settings` is the
one channel proven to survive that (see "Browsers and the virtual
display" above, where the same file carries the Playwright
artifact-reroute hook this box actually needs delivered), so whatever
puts that flag on the command line has to be the `claude` a human
session actually runs. Until now that was a nix
wrapper on the system PATH. `657fc21` pointed the wrapper at upstream's
self-updating install and appended, never prepended, `~/.local/bin` to
PATH, on the theory that only `environment.localBinInPath` could prepend
that directory. That was wrong within a day: the agent's own dotfiles
prepend it too (`~/.dotfiles/zshrc:187`), and `yo enter` ends with `exec
"$SHELL" -l`, an interactive login zsh that sources them. The first
`claude update` (2026-09-04 14:42, 2.1.259 → 2.1.260) put a bare launcher
ahead of the wrapper and ended herd reporting for every typed `claude`,
box-wide. `d8db855` answered by making the box the only claude
installation: DISABLE_AUTOUPDATER, a reaper that deleted any home install
within a second, and a rule saying no second claude may exist.

Both attempts were the same mistake in opposite directions — racing a
directory the box does not own, then forbidding anything from living in
it. The box now owns the path itself. `~/.local/bin/claude` is a tmpfiles
`L+` link to `/etc/yolobox/bin/claude`, a launcher script that picks the
newest binary under `~/.local/share/claude/versions/` and execs it with
`--settings /etc/claude-code/claude-hooks.json` ahead of `"$@"`, so a user's
own later `--settings` still wins. Anthropic documents (setup docs, since
2.1.207) that a custom launcher at that path is left alone by `claude
update` and by auto-update, which only drop new binaries into
`versions/`. So the dotfiles may prepend `~/.local/bin` all they like: the
first `claude` on PATH is the box's launcher either way, and `claude
update` now works and is welcome. DISABLE_AUTOUPDATER and the reaper are
gone.

The custom launcher costs one thing: claude prunes old versions only when
it owns the launcher, and each version is about 330 MB. So the same user
path unit that guards the link, `yolobox-claude-launcher`, also prunes
`versions/` to the two newest. It watches both `~/.local/bin` and
`versions/`, re-links within a second when `readlink` disagrees, writes
only when something is actually wrong (so the modification it causes
settles rather than looping), and logs the one line saying it did. The
tmpfiles rules at boot and on `switch` are the backstop for the window the
path unit cannot see — an install made while no user manager of the
agent's was running.

The installs themselves come from `yolobox-harness-install`, a user
oneshot gated `ConditionUser=agent` and wanted by `default.target`, so it
runs at boot rather than at the first `yo enter`. Each phase is
idempotent and checks before it acts: claude via `curl -fsSL
https://claude.ai/install.sh | bash -s latest` when `versions/` is empty,
then the marketplace and plugins (below); pi via `npm install -g
--ignore-scripts @earendil-works/pi-coding-agent` — the package nixpkgs
tracked, `@mariozechner/pi-coding-agent`, was deprecated in May 2026;
agent-browser via `npm install -g agent-browser@0.34.0` **with** scripts,
pinned because pi-agent-browser-native 0.5.0 refuses browser-backed calls
against any other agent-browser version, at call time; scripts run because
its postinstall is what downloads the binary, followed by a hard
`agent-browser --version` check, because that download fails silently;
opencode via its
own installer with `--no-modify-path`, into `~/.opencode/bin`, which a
tmpfiles link from `~/.local/bin/opencode` makes reachable (the installer
has no install-dir override). No vendor installer is ever allowed to edit
an rc file. `NPM_CONFIG_PREFIX=$HOME/.local` puts every `npm -g` binary in
`~/.local/bin` alongside the launcher, and `environment.localBinInPath`
puts that directory on PATH for non-login shells too — every
non-interactive `yo` guest call (`ssh_run`, behind `yo gc`, `yo status`
and the rest) and t3 both run in one. t3's own unit carries
`${homeDir}/.local` in its `path`, so a t3-spawned claude goes through the
launcher and carries the hooks like any other.

The agent account now lingers (`users.users.agent.linger`). That is what
makes "at boot" true: without it the agent's user manager starts at the
first login and stops at the last logout, so the install service, the
launcher path unit and the podman prune timer would all wait for a
session, and `/run/user/1000` would come and go underneath anything that
kept a socket there. With lingering, all three run from boot and that
runtime directory persists.

Recognise a broken launcher by the disagreement, not by an error, because
there is no error anywhere: an unwrapped claude works perfectly and
simply carries neither `HERDR_AGENT` nor the Playwright hook, silently.
The honest probe is to ask the two shells and follow the link:

```sh
zsh -lic 'command -v claude'         # what a human session gets
bash -lc 'command -v claude'         # what an ssh command gets
readlink -f ~/.local/bin/claude      # where the link actually ends
```

The first two must both answer `/home/agent/.local/bin/claude`, and the
third must be `/etc/yolobox/bin/claude`. During the 2026-09-04 incident
the two shells disagreed. The old `yo herd-check` — removed along with
the rest of the host-forwarded herd wiring, see "herdr: the VM runs its
own server, panes are real ptys" above — missed it throughout for the
same reason this probe exists: its guest half ran over a
non-interactive, non-login shell that never sources `~/.zshrc`, so the
check was measuring a shell nobody uses. This probe does not depend on
any herd wiring to run, which is why it is the one worth keeping.

## MCP: the vendors' own plugins, not files this repo renders

`nix/mcp.nix` is gone, and with it the three per-harness config files it
rendered, `yolobox-mcp-smoke`, the per-engine playwright wrappers, the
nixpkgs `playwright-mcp` and `context7-mcp` packages, the nix-built
`pi-mcp-adapter` with its `mcp-scripting` skill, and `~/.config/mcp/mcp.json`.
Declaring servers once and rendering them per harness was correct while
every harness needed a file; it stopped being correct once each vendor
grew its own registry, because the rendered files then compete with the
registry rather than feed it.

claude gets two plugins from the official marketplace, installed by
`yolobox-harness-install` with `--scope user` after registering
`anthropics/claude-plugins-official` (registration is required first on a
non-interactive box) and recorded in `~/.claude/settings.json` under
`enabledPlugins`: `context7@claude-plugins-official`, a remote HTTP MCP
with no local runtime at all, and `playwright@claude-plugins-official`,
which runs `npx @playwright/mcp@latest` and therefore wants network the
first time each version is used. Because that file is where the installs
are recorded, `~/.claude/settings.json` must not be a symlink into the
dotfiles checkout, or plugin installs write there instead.

pi gets no MCP at all. `@upstash/context7-pi` is Upstash's own pi package,
pure JS, exposing native pi tools; `pi-agent-browser-native` drives
Vercel's `agent-browser` CLI the same way. Both go in with `pi install`,
never by editing pi's settings file. The extension does not read
`AGENT_BROWSER_EXECUTABLE_PATH` itself, so the install service writes
`~/.pi/config/pi-agent-browser-native/config.json`
(`browser.executablePath`) with `jq`, because `pi install` does not put
the extension's own `pi-agent-browser-config` helper on PATH.

opencode keeps only its LSP entry, which `nix/lsp.nix` now renders to
`/etc/yolobox/lsp/opencode.json` and names through `OPENCODE_CONFIG`.

## pi's settings.json belongs to pi

pi rewrites `~/.pi/agent/settings.json` on its own and logs, rather than
raises, a failed write. A deleted `nix/pi.nix` (`cb5e863`) once linked it
to `/etc/static/...`; the module went, the dangling link stayed, and since
that file is the only place pi keeps its `packages` list, nothing from
`~/.dotfiles/pi/packages.json` installed. `pi list` said "No packages
installed" and `cc-compat` complained that `AskUserQuestion` was
unavailable on every start.

Recognise it with `ls -l ~/.pi/agent/settings.json`: a symlink is the
defect, `rm` is the fix. No tmpfiles rule can do this — there is no
"delete only if dangling", and an unconditional `r` would delete the real
file.

The VM has no C or Python toolchain, so an extension pulling a native
module without an aarch64 prebuild cannot install. That dropped
`@plannotator/pi-extension` (`node-pty` prebuilds darwin and win32 only).
Prebuilt glibc packages like `@ast-grep/napi` are fine.

Ownership: the flake owns nothing in pi's extensions tree any more. It
used to render one, `yolobox-agent-state.js`, pi's herd-report source
under the old design; pi's own herdr integration
(`~/.pi/agent/extensions/herdr-agent-state.ts`) replaces it now, and it
is vendor-installed with `herdr integration install pi`, not rendered by
the flake at all. `pi-mcp-adapter` and the `mcp-scripting` skill went
earlier, with `nix/mcp.nix`. Every one of these retirements needed
pairing with an `r` rule, and one didn't get it the first time: dropping
`yolobox-agent-state.js`'s `environment.etc` entry without an `r` rule
alongside it left the file a dangling symlink — a dropped `L+` link is
not removed by the rebuild that drops it, exactly the shape `pi-lsp`
below leaves behind, and exactly the shape this section already
documents for pi's own `settings.json`. Recognise it the same way, with
`ls -l`. The context7 and agent-browser packages are installed with `pi
install`, so pi owns them. Skills, agents, prompts, `cc-compat` and the
rest of the packages come from `~/.dotfiles/_do_install.sh`, re-run after
a dotfiles pull. Two extensions registering the same tool name kill pi at
startup — that retired the flake's pi-lsp extension in favour of
pi-lens. A retired `L+` link is not removed by the rebuild that drops it;
delete it by hand.

## t3: vendor-installed and self-updating, like claude and pi

t3code no longer comes from nix at all — `nix/pkgs/t3.nix` is gone. It
installs and updates itself from its own vendor script, into the agent's
home, the same as claude and pi (see "The harnesses come from their
vendors" above): the box owns only its environment, not the binary.
`yolobox-harness-install` (`nix/harnesses.nix`) runs `curl -fsSL
https://t3.codes/install.sh | bash` when `~/.local/bin/t3` is absent — the
installer never edits an rc file — and then, once, `t3 service install`
when `~/.config/systemd/user/t3code.service` does not yet exist. That
vendor command writes the real unit itself: `ExecStart` pointing at
whichever version the installer just unpacked under
`~/.t3/runtime/versions/<version>/`, `WorkingDirectory=%h`, and both
`StandardOutput`/`StandardError` appending to
`~/.t3/userdata/logs/boot-service.log`. `t3 update -y` — run by hand, or by
t3's own web UI once the boot service exists — downloads a new version the
same way and rewrites that same unit file, so a vendor update is simply
welcome, the same guarantee claude's own launcher already relies on for
`claude update`.

`nix/t3.nix` therefore owns nothing but a drop-in on top of that unit:
`systemd.user.services.t3code` with `overrideStrategy = "asDropin"` writes
only `/etc/systemd/user/t3code.service.d/overrides.conf` — there is no unit
file of that name in the nix store, so systemd's unit search finds the
vendor's own file under `~/.config/systemd/user` first and merges this
drop-in onto it, the same way NixOS already overrides an upstream
`systemd`-package unit like `user@.service` (see "Memory pressure" below).
Verify it directly: `systemctl --user cat t3code.service` (as the agent)
prints both fragments, vendor unit first, drop-in section second. The
drop-in pins `T3CODE_HOST=127.0.0.1` and `T3CODE_PORT=3773` — lima still
forwards 3773 the same way, so nothing downstream of the port changes —
and carries the agent's `SSH_AUTH_SOCK`/`AWS_CONFIG_FILE`
(`nix/lib/agent-env.nix`) plus `NIX_LD`/`NIX_LD_LIBRARY_PATH`
(`nix/lib/nix-ld-env.nix`): t3's own binary is a dynamically linked glibc
ELF, and nix-ld is what lets it run unpatched, the same reason the
harness-install script's own environment already needed those two
variables to run `t3 --version` at install time. `path` puts
`${homeDir}/.local` ahead of `/run/current-system/sw`, same reason as
before: a t3-spawned claude has to go through the box's own launcher to
pick up `HERDR_AGENT` and the Playwright settings file. Because the drop-in
carries `unitConfig.ConditionUser = agentUser` and no `wantedBy` of its
own, it never has to enable or start the unit itself — the vendor's `t3
service install` already did that, and re-enabling it is t3's job to do
again on every update, not this drop-in's.

Being a user unit, not a system one, changes where its logs and its status
live. `journalctl -u t3code` (no `--user`) and `systemctl show -p
ActiveState t3` as the operator now answer nothing at all — an empty
load-state, not an error — because that unit was never declared in the
system manager's namespace to begin with. `yo`'s own
`t3_require_service()` asks the agent instead: `systemctl --user show -p
LoadState -p ActiveState --value t3code.service`, over `ssh_run(AGENT,
...)`. That needs no pty and no login shell to work, because the agent
lingers (`users.users.agent.linger`, see "The harnesses come from their
vendors" above): logind pre-creates `/run/user/1000` at boot regardless of
any session, and sshd's PAM stack sets `XDG_RUNTIME_DIR` for the agent's
login on every connection, interactive or not — verified directly with a
bare `ssh -o User=agent ... systemctl --user show ...` and no env var set
by hand. The three failure shapes `t3_require_service()` now distinguishes
are: no answer at all (cannot reach the agent account — `yo status`); a
`LoadState` other than `loaded` (t3 was never installed as a service for
this agent — restart the install unit:
`yo ssh sudo -u agent XDG_RUNTIME_DIR=/run/user/1000 systemctl --user
restart yolobox-harness-install`); and an `ActiveState` other than
`active` (diagnose with `journalctl --user -u t3code` as the agent, or
tail `~/.t3/userdata/logs/boot-service.log` directly — the same file
either way, since the unit's own log redirection is what actually captures
t3's output).

`t3 serve`'s `$HOME` must equal an interactive session's: `t3 pair` finds
the server through `$HOME/.t3/*/server-runtime.json`, and a mismatch makes
`yo t3` say "No running T3 Code server found" while the unit is active.
The drop-in's `environment.HOME` is what keeps that true, the same as the
old system unit's did.

Nothing prunes `~/.t3/runtime/versions/` on its own — install.sh and `t3
update -y` both leave every version they ever downloaded behind, ~200 MB
each. `nix/t3.nix`'s `yolobox-t3-prune`, a `systemd.user.paths` unit
watching that directory the same way `yolobox-claude-launcher` watches
claude's own version store, keeps the two newest and never deletes
whichever version `~/.local/bin/t3` currently resolves to. A version in
flight is never a pruning target for a structural reason, not just the
10-minute settle window borrowed from claude's keeper: install.sh only
ever stages a download under a hidden `.staging-XXXXXX` name and
atomically `mv`s it to its real, non-hidden `<version>` name once the
archive is verified, extracted and already carrying `.install-complete` —
so any directory the prune's own `find` can see at all (it excludes
dotdirs) is by construction a completed install, never a partial one.

The forward is inert on an instance that already exists: lima copies
`lima/yolobox.yaml` into `~/.lima/yolobox/lima.yaml` at creation and reads
only that afterwards. Migrating means stopping and restating the whole
array, because lima's yq cannot read the repo file:

```sh
limactl stop yolobox
limactl edit yolobox --set '.portForwards = [{"guestPort":3773,"hostIP":"0.0.0.0"},{"proto":"udp","guestPort":68,"guestIP":"0.0.0.0","ignore":true}]'
./yo up
```

### Log noise that is not a failure

`Grok CLI health check failed` means no grok binary — still expected,
untouched by this rewrite. `Failed to flush telemetry` repeats every
second when the endpoint is unreachable — also untouched. The
"`linux-arm64` resource-monitor binary" line this section used to
describe no longer applies: the vendor's own release ships a
`linux-arm64` resource-monitor binary now, running unpatched under nix-ld
the same as `t3` itself, so the old "not supported on this platform"
message this section warned about should not appear any more — verified
by its absence from `journalctl --user -u t3code` (as the agent) across a
full restart, though the journal carried no distinct "monitoring started"
line either to confirm the positive case outright.

### Pairing

A bare URL fails auth; `t3 serve` prints a pairing URL in `journalctl -u
t3`, or `t3 pair` mints one. `t3 pair` builds the URL from the `--host`
the server started with, `127.0.0.1`, useless elsewhere and with no
override; only `t3 auth pairing create --base-url` takes one, which is why
`yo pair` is a separate command that defaults to
`http://<LocalHostName>.local:3773` and prints instead of opening.

Pairing runs client→server only; no server-to-server pairing exists. Two
boxes are driven from one browser: `yo pair` on each, both URLs redeemed
in the same browser, both appear in its environment list.

### An empty page means lima's forwarder died, not t3

From the Mac, `curl -v http://127.0.0.1:3773/` gets `Connection reset by
peer` with zero bytes, while inside the VM the same request returns 200.
That asymmetry is the diagnosis. `~/.lima/yolobox/ha.stderr.log` repeats
`tcpproxy: ... grpc: the client connection is closing`.

Lima 2.2.0 carries the guest-agent streams and the TCP tunnel on one grpc
connection. When the guest agent restarts, the host agent dials a new
connection, but an existing listener keeps its old dialer, so every
forwarded port accepts and then resets, permanently. It is a regression
from lima PR #4889, first shipped in v2.1.2, which closes the stale grpc
connection on reconnect; before that the stale connection leaked but still
carried traffic, so a box on lima older than v2.1.2 never showed this.
The host agent replaces the connection only when the guest agent is
still down 10 s after the event stream ended; a quick `systemctl restart
lima-guestagent` stays inside that window and is harmless, a `switch`
that re-runs `lima-init` first is not. Unreported upstream (see
`TODO.md`).

Why it bites here: `services.lima.enable` makes `lima-init` and
`lima-guestagent` ordinary units whose text embeds store paths, so a
nixpkgs bump rehashes them and `switch` restarts both. Twenty switches on
one pin never showed it; the first bump did. Pinned shut with
`restartIfChanged = false` on both units. `yo` could not have caught it —
every check ran on the healthy side of the hop, and a TCP-connect probe
succeeds because the handshake completes before the reset. Recovery:
`limactl stop yolobox && ./yo up`; nothing lighter restarts the host agent.

### `lsof` shows lima on `*:80` and `*:443`

On macOS a non-root process cannot bind `127.0.0.1:80` but can bind
`0.0.0.0:80`, so lima does that for any host port below 1024 and wraps it
in a listener that drops any non-loopback peer after the handshake. Not
exposure. The real cost: nothing else on the Mac can bind 80 or 443.

## Tailscale: t3 on the tailnet

`nix/tailscale.nix` puts the box on a tailnet so t3 (see "t3:
vendor-installed and self-updating, like claude and pi" above) is
reachable from outside the Mac's own
LAN, without touching the NixOS firewall. `tailscale0` stays closed:
`services.tailscale.openFirewall = true` opens only UDP 41641, the tunnel
port, never a hole for 3773. What actually reaches t3 is `tailscale serve
--bg --http=3773 http://127.0.0.1:3773`, run once at boot by the
`yolobox-tailscale-serve` oneshot, which runs `tailscale serve reset`
first so the unit owns the whole serve config: an entry left behind under
an earlier tailnet name — a login made with a hand-typed `--hostname`,
say — is dropped rather than served forever next to the current one. Serve is tailscaled's own reverse
proxy, terminating on the tailnet interface and forwarding to the
loopback port, so the request never needs a firewall rule to cross
`tailscale0` at all. In TUN mode (the default; nothing here sets
`interfaceName = "userspace-networking"`) tailscaled owns that traffic
directly.

`--accept-dns=false` in `extraSetFlags` stops MagicDNS from taking over
`/etc/resolv.conf`. Left at tailscale's default, the guest's DNS would
answer through the tailnet's resolver for every name, not only
`*.ts.net` ones — silently changing what every other process in the box
resolves, including the Mac's own DNS filter this repo's AWS section
already relies on staying in place (see "AWS credentials" below). Setting
it false keeps DNS exactly as every other section here assumes it is; the
box's own name is still reachable as `yolobox.<tailnet>.ts.net` by MagicDNS
running on the *client* end of a tailnet connection, never by this guest's
own resolver.

Login is interactive and happens once, by the operator, because there is
no auth key anywhere in this repo: `yo ssh sudo tailscale up`. It takes
no `--hostname`: `extraSetFlags` pins the tailnet name to
`networking.hostName`, so every box joins as `yolobox`, and a hand-typed
`--hostname` is overwritten by the next `tailscaled-set` run anyway.
State from that login — the node key, the tailnet
identity — persists under `/var/lib/tailscale`, ordinary VM-local state
like everything else under "Two accounts" above: it does not survive
recreating the VM, and a recreated box logs in again the same way.

The failure shape has no ambiguity built into it on purpose: a box that
has never logged in, or whose login has expired, makes
`yolobox-tailscale-serve` fail rather than quietly serve nothing or retry
forever. The unit reads `tailscale status --json | jq -r .BackendState`;
anything other than `Running` is exit 1 with the exact remedy on stderr —
`yo ssh sudo tailscale up`, then `yo ssh sudo
systemctl restart yolobox-tailscale-serve` — never a sleep loop chasing a
state that a `tailscale up` run once was always going to settle. `t3`
itself never depends on any of this; it keeps listening on loopback
whether or not the tailnet is up, so t3 reached from inside the VM or
through lima's own Mac-side forward is unaffected by a tailscale login
that has not happened yet.

The first switch into a generation with this module restarts dhcpcd,
because nixpkgs' tailscale module adds `tailscale0` to
`networking.dhcpcd.denyInterfaces`, which changes dhcpcd's config. On stop
dhcpcd removes the lease address, and the kernel flushes every route that
hung off it — including any static route a `/etc/yolobox/local.nix`
declared through `networking.interfaces.<if>.ipv4.routes`, whose
`network-addresses-<if>` unit runs once and never re-adds it. The switch
reports success and the route is simply gone. Any later dhcpcd restart (a
nixpkgs bump) does the same, so a box-local route belongs in
`networking.dhcpcd.runHook`, re-added with `ip route replace` on every
`BOUND`/`REBOOT`/`RENEW`/`REBIND`, where its lifetime follows the lease
it depends on.

Once logged in, `yo pair http://yolobox.<tailnet>.ts.net:3773` mints a
pairing URL against the tailnet name instead of the default
`<LocalHostName>.local` one (see "Pairing" above) — the same command, the
only difference is which base URL reaches the server. That address works
from any device already joined to the tailnet, on any network, which is
the entire point of putting t3 there rather than only on the Mac's LAN.

## far2l: TTY-only, S3 through the broker

far2l (FAR Manager's Linux port) is on both accounts' PATH from
`nix/base.nix`'s `environment.systemPackages`, through `nix/pkgs/far2l.nix`,
an override of nixpkgs' own `far2l`. Nixpkgs builds NetRocks with openssl,
libssh, samba, libnfs and neon, but never passes `aws-sdk-cpp`, and far2l
looks for it with `find_package(AWSSDK QUIET COMPONENTS s3)` — so the stock
package ships with no S3 and says so only in a cmake warning. The override
adds `aws-sdk-cpp.override { apis = [ "s3" ]; }`, only core and s3 rather
than the SDK's hundreds of APIs.

Every NetRocks backend is gated the same quiet way, so a nixpkgs bump that
drops a dependency would ship a far2l missing that protocol with no error
anywhere. That is why the override's `postInstall` checks that each broker
exists under `lib/far2l/Plugins/NetRocks/plug/` — FILE, SHELL, FTP, SFTP,
SMB, NFS, WebDAV, AWS — and that the FTP broker links OpenSSL, since FTPS
is not a broker of its own but only OpenSSL linked into `NetRocks-FTP`.
Recognise the failure by `far2l: NetRocks-<proto> missing` (or the FTPS
message) in a `nixos-rebuild` log: the fix is restoring the dependency, not
dropping the check.

`withGUI = false` and `withTTYX = false`: `DISPLAY=:0` is set box-wide and
points at an Xvfb nobody sees (see "Browsers and the virtual display"), so a
wx GUI build would open its window there; TTYX would route the clipboard and
X key-modifier detection through that same invisible X server, reading the
Xvfb's keyboard state, not the Mac's. Plain TTY mode leaves the clipboard to
the terminal through OSC52, which does reach the Mac.

Cost: the override changes far2l's derivation hash, so far2l itself never
comes from cache.nixos.org; the box compiles it locally, about 1m40s, and
again after every nixpkgs bump. The s3-only SDK is not a local build:
Hydra caches `aws-sdk-cpp` with `apis = [ "s3" ]` too, verified by the first
build here fetching it rather than compiling it.

`far2l --tty --help` run with no tty attached never prints help: it
detaches, reparents to init, and sits there forever. Plain `far2l --help`
prints and exits; use that as a smoke test.

S3 usage: leave the NetRocks site's login and password both empty; far2l
then uses the SDK's `DefaultAWSCredentialsProviderChain`, which reads
`AWS_CONFIG_FILE` and `AWS_PROFILE` and runs `credential_process` — exactly
the guest config the AWS broker already renders (see "AWS credentials"
below). So `AWS_PROFILE=<P> far2l` gets that profile's broker credentials,
fixed for the life of that far2l process, the same per-process rule as every
other AWS client here. Filling in only one of login/password is refused by
far2l itself. Set the site's Region field, or leave it empty to fall back to
the SDK's default region resolution; the site's host doubles as a custom
endpoint (MinIO and the like).

## AWS credentials: a per-profile broker, selected per process by `AWS_PROFILE`

AWS access is no longer opt-in per `yo enter`, and no longer one profile
switched mid-session. v1 minted STS creds once, at enter time, and froze
them in the guest session's flat env (`AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`) — a session outliving the
STS duration needed a fresh `yo enter`. v2 kept a broker but still bound
it to one profile per pane, forwarded in as botocore's container
credential provider — a bearer token plus a URL, reached over a
per-session TCP `-R` to the guest's gateway address. The current design
makes every allow-listed profile available everywhere the VM runs
instead, lets each process pick its own with an ordinary
`AWS_PROFILE=<name>`, and — after a live test found the gateway-address
TCP path itself fragile under a VPN (see "Why a reverse unix socket, not
a forwarded TCP port" below) — reaches the broker the same way the agent
already reaches 1Password: a reverse unix-socket forward that rides
lima's own ssh connection, healed by the same mechanism (see "SSH
identities and the two GitHub accounts" above), never a TCP port of its
own.

**Why not one switchable broker profile.** botocore fixes a process's
credential source at first use, and only refreshes it 10–15 minutes
before the cached credentials actually expire — it never re-reads which
profile it should be using. A single global profile the operator flips
mid-session races on that fact: a running terraform or boto3 process
silently lands in the other account at its next refresh, tens of minutes
after the switch; two panes started at different times disagree about
"the current profile" for as long as that window lasts; the broker has to
keep serving the old profile's cache until only minutes are left on it,
rather than cutting over the instant the operator asks; and the region
drifts out from under the account, reopening the DNS sinkhole shape
described below. Fixing the profile at process start sidesteps all four —
nothing is ever switched under a process that is still running.

**The allowlist.** `aws_allowlist()` in `yo` reads the Mac's
`~/.aws/config` with `configparser` and allow-lists every `[profile X]`
and `[default]` section that carries `sso_session` or `sso_start_url`
(`[sso-session …]` sections themselves are excluded — they describe a
session, not an account to assume). A profile name is checked against
`AWS_PROFILE_NAME_RE` (`^[A-Za-z0-9._-]+$`) first, and one that fails —
anything with a space, a shell metacharacter, or other punctuation `yo`
will not pass down to `credential_process P` unescaped — is excluded with
a stderr line naming it, before the region is even looked at. A profile
with no `region` is left out the same way, with a stderr line naming it,
rather than reopening the DNS sinkhole below; a malformed config file
(bad ini syntax, undecodable bytes) raises instead of allow-listing
whatever partial read it got; no `aws` CLI or no SSO profiles at all means
one stderr line and no broker started.

**The guest config.** `yo` renders
`/home/agent/.config/yolobox/aws-config` — the file
`AWS_CONFIG_FILE` in `nix/lib/agent-env.nix` points every agent session
at — with one section per allow-listed profile:

```
[profile P]
region = R
credential_process = yolobox-guest aws-creds P
```

`AWS_CONFIG_FILE` replacing the guest's own AWS config path means an
agent-written `~/.aws/config` inside the VM is simply not read any more;
`yo up` warns on stderr when one is found, so its presence is at least
visible rather than silently ignored. `AWS_PROFILE=P aws ...` is then
the whole of how a process picks an account. `push_aws_guest_files()`
writes this file atomically — `mkdir -p` the config directory, `cat` the
rendered text into a `mktemp` sibling, then `mv` it over the real path —
so a `credential_process` invocation can never observe a half-written
config. When the allowlist empties out entirely (no `aws` CLI on the Mac,
or no SSO profiles left in `~/.aws/config`), `disable_aws_broker()` stops
any broker still running and removes this file from the guest rather than
leaving a stale one behind, and `yo status` reports `aws: disabled (...)`
instead of checking a broker that no longer exists.

**Why `credential_process`, not the container credential provider.**
v2 used botocore's container credential provider —
`AWS_CONTAINER_CREDENTIALS_FULL_URI` plus a bearer token — which botocore
only ever dials as an HTTP(S) URI; it has no unix-socket form at all, so
once the broker moved to a unix socket (below) the container provider
stopped being a candidate regardless of loopback or bearer-token
questions. `credential_process` carries no such restriction: it runs an
arbitrary command and reads its stdout as process-credential JSON, so
`yolobox-guest aws-creds P` is free to `curl --unix-socket` a Mac-side
broker exactly the way `ssh-add` already reaches a Mac-side 1Password
over the same shape of forward.

**Why a reverse unix socket, not a forwarded TCP port.** A live test on
2026-09-28 found ProtonVPN, connected on the Mac, blackholing every guest
connection to `192.168.5.2:<port>` — gvproxy's NAT of lima's own gateway
address to the Mac's loopback, the path v2's TCP `-R` and the address
above both relied on — while the guest's general egress, and even `ssh
git@github.com`, kept working throughout. Lima's reverse unix-socket
forwards ride lima's own ssh connection instead of a second, separately
NATed TCP path, and were unaffected by that same test. That is exactly
the shape 1Password's own forward already used (see "SSH identities and
the two GitHub accounts" above), so the AWS broker takes the same shape
now rather than a second, VPN-fragile one of its own: `aws-broker` binds
a Mac-side unix socket, `~/.local/state/yolobox/aws-broker.sock` (born
`0600` by a tightened umask; a stale socket from a broker that died
without cleaning up is unlinked, but a non-socket file at that path is
refused outright, never removed), lima reverses it to
`/run/yolobox-op/aws-broker.sock` in the guest — bound as the operator,
lima's cidata account, exactly like 1Password's own reverse forward — and
`nix/base.nix`'s `opProxy` (the same chokepoint that already proxied
1Password's socket) bridges it through `systemd-socket-proxyd` to an
agent-owned `/run/yolobox/aws-broker.sock`. No bearer token guards any of
it: the filesystem permissions on that last socket are the entire
authorization boundary, the same as 1Password's.

**The broker's own lifetime rides lima's hostagent, never a per-session
forward or an idle timeout.** `aws-broker --watch-pid <pid>` uses
`select.kqueue` with `EVFILT_PROC`/`NOTE_EXIT` to notice the instant
lima's hostagent process exits, rather than polling for it — the broker
now outlives any one `yo enter` pane by design, since profiles are chosen
per process rather than per session, and there is no session left to tie
its life to. That pid comes from `ha_pid()`, which reads `hostAgentPID`
out of `limactl list yolobox --json` — never by opening
`~/.lima/yolobox/ha.pid` directly, the file whose mtime "Two accounts"
above already flags as something lima itself rewrites on every start.
`ensure_aws_broker()` in `yo` checks `GET /health` first, through
`broker_needs_restart()`: a running broker is left alone only when
*both* its reported `watch_pid` still matches today's hostagent pid *and*
its reported `allow` list still equals today's sorted allow-listed profile
names — either a hostagent restart (a new `yo up`) or an edited
`~/.aws/config` between two `yo` invocations forces a fresh broker rather
than serving a broker whose profile set is now wrong. A stale broker is
SIGTERM'd and waited out with the same kqueue mechanism before the fresh
one starts; its stderr is truncated and reopened at
`state_dir()/aws-broker/broker.log` on every start (never appended to a
growing file), and its stdout carries exactly one handshake line, now
just `READY`, before it `os.dup2`s `/dev/null` onto itself — never write a
secret to stdout after that line, or to any log. The bind itself refuses
to steal a live socket: `reclaim_socket_path()` probes a connect before
unlinking anything and fails outright, naming the collision, if something
is genuinely still being served there; at shutdown (both the normal
`finally` around `serve_forever()` and the watch-pid thread's exit path),
`unlink_if_same_identity()` removes the socket file only when its
`(st_dev, st_ino)` still match the pair this same broker recorded at
bind time, so a superseded broker exiting late can never delete a newer
broker's socket out from under it.

Minting stays lazy and per-profile, each with its own lock, cache,
throttle and last error, so one profile's expired SSO session never
blocks another's `/creds/<P>` request; the long-lived-key refusal (an IAM
user's keys, which never expire and must never enter the guest this way)
stays a request-time 500 naming the fix. `aws sso login --profile P` on
the Mac heals a running guest session with no re-enter, exactly as under
v1 and v2 — the next `/creds/<P>` hit just re-mints successfully.

**There is exactly one broker socket to reach now, not one URL per
profile**, so there is no longer a port or a gateway address for `yo` and
the guest side to keep in sync — `credential_process` takes only the
profile name (above), and the guest script no longer hardcodes the socket
path itself. `nix/lib/agent-env.nix`'s `awsBrokerSock` is the one place
that names the agent-facing path, `/run/yolobox/aws-broker.sock`; it sits
outside the `env` attrset the same file also exports, because it names a
proxy target (`nix/base.nix`'s `opProxy`, `nix/guest.nix`'s guest script),
not a variable an agent process should itself export. `nix/guest.nix`
folds it into `YOLOBOX_AWS_BROKER_SOCK` through `writeShellApplication`'s
`runtimeEnv`, so `nix/guest/yolobox-guest.sh`'s `cmd_aws_creds` reads it
from the environment rather than spelling the path a second time. What
still has to be kept in sync by hand — because it is the *Mac-side* half
of the same path, not the guest-side half `agent-env.nix` already
chokepoints — is: `yo` (`AWS_BROKER_GUEST_SOCK`, folded into
`LIMA_PORT_FORWARDS`, and `AGENT_AWS_BROKER_SOCK`, its own health probe's
target — checked against `agent-env.nix`'s text by a unit test,
`TestAgentEnvMatchesYoSockets`, so the two constants can never drift
silently) and `lima/yolobox.yaml` (the one-time seed for a brand-new
instance, checked against `LIMA_PORT_FORWARDS` by another test,
`TestLimaYamlMatchesPortForwardsConstant`). `lima_config_gaps()` (see
"SSH identities and the two GitHub accounts" above) is what catches the
lima side drifting at runtime, on every `vm_up()` — as a stderr note now,
never a refusal, since `heal_reverse_forward()` adds the forward live
regardless — rather than only at test time.

**`aws-creds`, in `nix/guest/yolobox-guest.sh`:** `curl -sS
--fail-with-body --unix-socket "${YOLOBOX_AWS_BROKER_SOCK}"
"http://broker/creds/${profile}"`, printing the body and curl's own error
to stderr on failure. Three of curl's own exit codes, not one, are
treated as "the broker is unreachable" and given the `yo up`-on-the-Mac
remedy rather than curl's generic message: 7 (connection refused, meaning
the proxy *socket unit itself* is absent — an old box) and also 52 and 56
(empty reply, connection reset). 52/56 are the common case here, not the
rare one, for the same reason `op_probe_alive` above already has to
distinguish two exit-1 shapes: the agent-side proxy socket
(`yolobox-aws-broker.socket`) always accepts a connection whether or not
anything real is listening behind lima's forward, so a dead forward or a
dead Mac-side broker reads back as a broken connection mid-request, not a
refused one. `yo aws-check` is the doctor now too: it runs
`ensure_aws_broker()` for real, then for each profile section in the
guest's `AWS_CONFIG_FILE` runs `AWS_PROFILE=P aws sts
get-caller-identity` in the VM and asserts a role ARN comes back.

A DNS failure shape met on the very first v1 launch still applies
unchanged: credentials arrive but `aws sts get-caller-identity` dies with
`Could not connect to the endpoint URL: "https://sts.amazonaws.com/"`.
That is DNS, not AWS: this Mac's DNS filter sinkholes the *global*
`sts.amazonaws.com` name to 0.0.0.0, the guest inherits the Mac's resolver
through lima's forwarder, and the CLI only targets global endpoints when
it has no region. Regional endpoints (`sts.eu-west-1.amazonaws.com`)
resolve fine on both sides — which is exactly why a region-less profile
is excluded from the allowlist above rather than shipped broken.
