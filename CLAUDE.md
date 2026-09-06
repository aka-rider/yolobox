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
  `yo --help` describes each command.
- `flake.nix` — wires the modules together and threads the host username
  in.
- `nix/base.nix` — filesystems, boot, sshd, tmpfiles, git defaults, the
  lima units, core packages.
- `nix/podman.nix` — rootless podman with a docker-compatible socket.
- `nix/harnesses.nix`, `nix/agentic.nix` — the coding agents: the
  box-owned `claude` launcher, the user service that installs claude, pi,
  opencode and agent-browser from their vendors, the user path unit that
  re-asserts the launcher, and nix-built herdr.
- `nix/herd-report.nix` — how in-VM agents report to the Mac's herd.
- `nix/display.nix` — the virtual X display, browsers, screen recording.
- `nix/lsp.nix` — the Python language-server wiring for every editor.
- `nix/t3.nix`, `nix/pkgs/t3.nix` — t3code built from npm and run as a
  service.
- `nix/pkgs/` — packages nixpkgs lacks or lags on.
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
no other route in. `xvfb`, `openbox` and `t3` are system units that run
*as* the agent, so their logs land in the system journal, not some user
journal the agent already owns by default. Hence the agent carries
`extraGroups = [ "systemd-journal" ]` on top of dropping `wheel` — without
it, `journalctl -u t3` silently narrows to only the agent's own messages,
which is also what the remedy `yo` itself prints when t3 looks missing, so
the diagnostic advice would have quietly stopped working too.

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
the split: the forwarded 1Password agent still lets an agent session
authenticate to GitHub as the operator during any `yo enter`; the AWS
broker still hands out real credentials whenever `YOLOBOX_AWS_PROFILE` is
set, by design; every commit an agent makes still rides the git push
channel back to the Mac, where the operator is the one who builds and
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

`ForwardAgent yes` forwards `$SSH_AUTH_SOCK`, which on this Mac is Apple's
launchd agent holding nothing; the real keys live in 1Password. So `yo`
hands `ForwardAgent` the 1Password socket path itself, and only `yo enter`
and `yo ssh` ever request it — each over its own `ControlPath=none`
connection, never a shared, persistent `ControlMaster`. So the forward is
scoped to that one interactive session: it dies with the pane, `yo
code`/`yo zed`/a bare `ssh lima-yolobox` never inherit it, and the old `-O
exit` dance to un-stick a forward stuck on a shared master no longer
applies — the forward never rides a shared master to begin with.

That scoping is also why `ForwardAgent` deliberately does not go into
`~/.lima/yolobox/ssh.config`, the file `yo` writes and re-applies (see
"Two accounts" above). `yo` runs all of its own ssh with `-F
~/.lima/yolobox/ssh.config`, so a `ForwardAgent yes` sitting in that file
would hand the 1Password agent to `yo code`, `yo zed` and every
t3-spawned session too — exactly the scoping this section just described
as deliberate. A `Host lima-yolobox` / `ForwardAgent yes` block sitting in
`~/.ssh/config` instead is in any case a no-op on this Mac: `SSH_AUTH_SOCK`
points at Apple's launchd agent, and `ssh-add -l` against it reports "The
agent has no identities" — the real keys live in 1Password, which is why
`yo enter` and `yo ssh` pass `-o ForwardAgent="<1Password socket>"` per
connection instead of relying on any config file to carry it.

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

`SendEnv` is another value that must never ride a shared `ControlMaster`.
Measured 2026-09-04 against the running box, same connection, only the
`ControlPath` differing: over `~/.lima/yolobox/ssh-agent.sock` the guest
saw none of the forwarded variables (`YOLOBOX_HERD`, `HERDR_PANE_ID`,
`HERDR_SOCKET_PATH` in that test); with `ControlPath=none` all three
arrived. The mux drops it with no error anywhere — the `-R` socket forward
still works over the same shared connection, so the session looks wired:
the socket appears in `/run/yolobox`, the reporter runs, sees none of its
env, and exits 0 without logging. `ssh_run` now refuses to combine
`mux=True` with any `SendEnv` option, rather than let the next caller who
adds one to a multiplexed call rediscover this by watching a report vanish
silently.

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
`<group>/<repo>` — a monorepo puts one at depth 7. So the walk in
`vm_project_pick` is bounded by pruning `node_modules`, `target`, `.next`
and every hidden directory (`.git` itself included), not by a depth cap,
because a depth cap has to be re-guessed every time a monorepo nests one
level deeper. Recognise
the failure by its shape: a cap surfaces as fzf reporting "no project matches"
for a repo that plainly exists, so what to check is the list handed to fzf, not
fzf. `yo gc`'s `project_targets` carried the same cap, which made `--deep`
blind to build directories inside monorepo projects.

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
  guard for every subcommand except `--help` and `--version`, so `nix run
  github:aka-rider/yolobox` inside a guest fails loudly, never with a bare
  `command not found`.
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
links, which `nix/herd-report.nix` relies on to fill pi's auto-discovery
directories.

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
output under `~/artifacts` exists to prevent. `nix/herd-report.nix`'s
second `PreToolUse` hook, `yolobox-playwright-artifacts`, catches this
before the tool runs: it rewrites `filename` in the tool input, keeping a
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

## Herd reporting: recognising a silent failure

Only claude (a hook map) and pi (a bundled extension) report into the
herd; see `nix/herd-report.nix`. An unreported agent runs fine and shows as
`unknown`. Reporting rides `yo enter` only — it forwards the herdr socket
to `/run/yolobox/herd-host.<pane>.sock` and sets the herd env. `yo ssh`,
`yo code`, `yo zed` and t3-spawned agents carry neither, so they show as
`unknown` by design.

A herdr-installed `~/.pi/agent/extensions/herdr-agent-state.ts` reports
under `herdr:pi`, a source the server reserves for itself, and knocks pi
out of the herd. A tmpfiles `r` rule deletes it on every boot and switch.

Two defects once made every in-VM claude session invisible.

**Version drift.** Homebrew herdr moved to 0.8.2 (protocol 20) while the
box was pinned at 0.8.0 (protocol 19), and every report was rejected with
`protocol_mismatch`. The protocol moved across a patch bump, so only exact
version equality is an honest proxy. The pin is gone: the box tracks
nixpkgs' `pkgs.herdr`, with `yolobox.harness.herdr.version`/`hash` as an
escape hatch for when nixpkgs lags. `yo enter` refuses on drift (exit 3;
`YOLOBOX_SKIP_HERD_CHECK=1` overrides, needed when nixpkgs has no matching
version yet), `yo status` warns, `yo herd-check` proves the chain.

**The hooks never ran.** Claude Code assembles policy settings from three
tiers — remote, MDM, `/etc/claude-code/managed-settings.json` — and takes
only the first non-empty one, no merge. `~/.claude/remote-settings.json`
holds `{"channelsEnabled": true}`, which is enough to make the remote tier
win and drop the `/etc` file whole, with no log line. The remote payload is
re-applied about 180 ms into every session, so even an empty cache would
not help. The policy channel is therefore unusable. The fix is the
box-owned `~/.local/bin/claude` launcher, which puts `--settings
/etc/claude-code/herd-hooks.json` in front of every invocation (see "The
harnesses come from their vendors; the box owns `~/.local/bin/claude`"
below); that channel is always honoured and was verified to survive the
mid-session refresh. That same file also carries the Playwright
artifact-reroute hook (see "Browsers and the virtual display" above) —
one settings file, two unrelated `PreToolUse` entries, both riding the one
channel proven to survive the refresh.

The absence of `~/.local/state/yolobox/herd-report.log` proves nothing: a
hook that never runs writes nothing. When an agent is missing from the
herd, work down this ladder:

1. `ls -l /run/yolobox/` — no socket means the `-R` forward failed to bind,
   and ssh said so at connect time; usually `/run/yolobox` missing after a
   switch with no reboot.
2. `sudo strace -f -qq -e trace=execve -p <claude pid>` grepped for
   `herd-report` — do the hooks run at all. `sudo`, because
   `ptrace_scope=1`.
3. `claude --debug` grepped for `Hook output` / `policySettings` — which
   channel won.
4. `herdr pane report-agent <pane> --source yolobox:x --agent claude-vm
   --state idle` landing while the same call with `--agent claude` does not
   proves the process-exit latch below (release both afterwards).

A measurement trap that fooled this twice: `claude -p '<prompt>'` runs the
whole lifecycle in two seconds — `SessionStart`, `UserPromptSubmit`,
`Stop`, `SessionEnd` — and `SessionEnd` releases the agent from the herd.
So a headless run followed by a look at the herd shows nothing, exactly
like hooks that never ran. Use an interactive `claude`, read the log, or
watch the herd while the run is in flight. A leftover socket from a dead
`yo enter` answers `server_not_running` and is harmless;
`StreamLocalBindUnlink` lets the next `yo enter` rebind it.

**The process-exit latch.** A third shape shows up per-pane rather than
box-wide: `yo enter` then `claude` in the guest, and that one session never
appears in herdr's agents list, while a second pane started the normal way
works fine. Both launches are identical — same launcher, the same
`YOLOBOX_HERD`/`HERDR_PANE_ID`/`HERDR_SOCKET_PATH`, the same forwarded
socket answering `herdr status`, and no rejection anywhere in
`~/.local/state/yolobox/herd-report.log` — because there is none to log:
herdr's `report-agent` answers `ok` regardless. The discriminator is the
pane, not the agent or the box: only a pane in which a *host-side* claude
process had run and exited stays blind; a fresh split pane works, and pi
in that same blind pane still reports fine. It first looked like "the
first agent anywhere", because the box usually has no agent running yet
right when a host-side claude has just quit — coincidence, not cause:
herdr's detection is per pane (`src/pane.rs:2156-2178`), with no notion of
a global agent count anywhere. The mechanism is a latch:
`TerminalState::set_hook_authority_at` (`state.rs:633-652`) discards the
report — returns `None`, while `report-agent` still answers `ok`
(`src/app/api/panes.rs:1231-1257`) — whenever `recent_agent_process_exit`
names the same agent kind the report carries, and that latch is armed by
host-side process detection (`state.rs:399-405`) with no expiry: the only
time-windowed check, `agent_process_exited_within` (`state.rs:249`), is
compiled only for Windows and tests. It clears solely when detection sees
that agent kind again or the pane's shell respawns — never on an `ssh`
foreground process, which is exactly what a guest session leaves in place
for as long as the pane lives. Six full-lifecycle hook sources
(`herdr:pi` among them) are exempt by name; a hook-reported `claude` is
not. Full writeup, with the repro and every file:line:
`upstream/herdr-process-exit-latch.md`.

Short of the upstream fix, `yo enter` now works around it: it reports a
throwaway `claude` probe into the pane before connecting, reads it back,
releases it, and warns on stderr naming the cause and the remedy (open a
fresh pane or tab) when the read-back disagrees — then continues, because
pi is unaffected. The guest reporter's `SessionStart` does the same
read-back after its real report and fails loudly in claude's own UI when
it was dropped, rather than leaving silence for the next person to misread
as "the hooks never ran". `yo herd-check` stage 3 names this cause
specifically, rather than stopping at "which channel won".

The reporter always exits 0, with one exception: `SessionStart` exits 1 on
a failed report and writes the diagnostic to stderr, where Claude Code
surfaces it. It is off the work path, so it cannot interrupt anything;
`PreToolUse` fires on every tool call and would turn one broken socket
into a wall of warnings. Each log line carries the guest herdr version and
socket path.

`yo herd-check` proves the chain end to end. Run it from a herdr pane with
no agent on it: it reports and then releases that pane.

Do not add age-based tmpfiles cleanup for stale sockets: `relatime` caps
the atime refresh at about a day and an established connection touches
nothing, so an age would reap a live pane's socket.

Grep gotcha: filtering `/proc/<pid>/environ` needs
`grep -E '^(YOLOBOX_HERD|HERDR_)'`. Writing `'^(HOME|HERDR_|PATH)='` binds
the `=` after the alternation and matches nothing.

## The harnesses come from their vendors; the box owns `~/.local/bin/claude`

claude, pi, opencode and agent-browser are not nixpkgs packages any more.
They install into the agent's home from their own vendors and self-update
there, and the box gives up on owning their versions. What the box owns
instead is one path: `~/.local/bin/claude`. That inversion is the whole
design, and the reason for it is an outage this file used to describe from
the other side.

The herd hook map reaches claude only as `--settings <file>` — see "Herd
reporting" above for why the `/etc/claude-code/managed-settings.json` tier
cannot carry it — so whatever puts that flag on the command line has to be
the `claude` a human session actually runs. Until now that was a nix
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
`--settings /etc/claude-code/herd-hooks.json` ahead of `"$@"`, so a user's
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
puts that directory on PATH for non-login shells too — `yo herd-check`'s
`ssh_run` and t3 both run in one. t3's own unit carries
`${homeDir}/.local` in its `path`, so a t3-spawned claude goes through the
launcher and carries the hooks like any other.

The agent account now lingers (`users.users.agent.linger`). That is what
makes "at boot" true: without it the agent's user manager starts at the
first login and stops at the last logout, so the install service, the
launcher path unit and the podman prune timer would all wait for a
session, and `/run/user/1000` would come and go underneath anything that
kept a socket there. With lingering, all three run from boot and that
runtime directory persists.

Recognise a broken hook chain by the disagreement, not by an error,
because there is no error anywhere: an unwrapped claude works perfectly
and simply carries no hooks, and a reporter that never runs logs nothing
— `~/.local/state/yolobox/herd-report.log` does not record a failure, it
just stops. The honest probe is to ask the two shells and follow the link:

```sh
zsh -lic 'command -v claude'         # what a human session gets
bash -lc 'command -v claude'         # what an ssh command gets
readlink -f ~/.local/bin/claude      # where the link actually ends
```

The first two must both answer `/home/agent/.local/bin/claude`, and the
third must be `/etc/yolobox/bin/claude`. During the 2026-09-04 incident
the two shells disagreed, which is also why `yo herd-check` passed
throughout: its guest half ran over a non-interactive, non-login shell
that never sources `~/.zshrc`, so stage 6 was measuring a shell nobody
uses. Stage 6 now resolves claude through the account's login shell and
asserts the link target as well.

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

Ownership: the flake owns exactly one thing in pi's tree now, the
herd-report extension. `pi-mcp-adapter` and the `mcp-scripting` skill went
with `nix/mcp.nix`; their `L+` links are removed by `r` rules, because a
dropped link is not removed by the rebuild that drops it. The context7 and
agent-browser packages are installed with `pi install`, so pi owns them.
Skills, agents, prompts, `cc-compat` and the rest of the packages come
from `~/.dotfiles/_do_install.sh`, re-run after a dotfiles pull. Two extensions registering the same tool name kill pi at startup —
that retired the flake's pi-lsp extension in favour of pi-lens. A retired
`L+` link is not removed by the rebuild that drops it; delete it by hand.

## t3: a nix-built npm CLI, run as a service

`nix/pkgs/t3.nix` builds `t3` from its npm tarball; `nix/t3.nix` runs `t3
serve --host 127.0.0.1 --port 3773` as the guest account. Loopback in the
guest, but lima forwards 3773 with `hostIP: "0.0.0.0"`, dialing from
inside the guest, so the guest firewall is never traversed.

`t3 serve`'s `$HOME` must equal an interactive session's: `t3 pair` finds
the server through `$HOME/.t3/*/server-runtime.json`, and a mismatch makes
`yo t3` say "No running T3 Code server found" while the unit is active.
The unit gets `HOME` and a `path` entry for `/run/current-system/sw`, using
the `path` option because `environment.PATH` is already defined for every
service.

The forward is inert on an instance that already exists: lima copies
`lima/yolobox.yaml` into `~/.lima/yolobox/lima.yaml` at creation and reads
only that afterwards. Migrating means stopping and restating the whole
array, because lima's yq cannot read the repo file:

```sh
limactl stop yolobox
limactl edit yolobox --set '.portForwards = [{"guestPort":3773,"hostIP":"0.0.0.0"},{"proto":"udp","guestPort":68,"guestIP":"0.0.0.0","ignore":true}]'
./yo up
```

### Why package.json is patched

The published `package.json` carries pnpm-style `overrides` with
`parent>child` keys, which npm rejects with `EINVALIDPACKAGENAME` once the
package is the install root. `postPatch` deletes the field with `jq`,
called by store path, because the same `postPatch` runs inside the
npm-deps fixed-output derivation, which does not inherit
`nativeBuildInputs`. The vendored `t3-package-lock.json` must be generated
from the stripped file. Version bump: pin the tarball hash; unpack, `jq
'del(.overrides)'`, `npm install --package-lock-only --ignore-scripts`,
vendor the lock; build with a wrong `npmDepsHash` and pin what nix reports.

### dist/bin.mjs is no longer patched

t3's Claude probe hardcodes `strictMcpConfig: true`, which Claude Code
refuses whenever an enterprise MCP config exists. That used to bite here:
`nix/mcp.nix` once rendered one at `/etc/claude-code/managed-mcp.json`, t3
swallowed the resulting failure completely with nothing reaching journald,
and the only evidence was `~/.t3/caches/claudeAgent.json` reading
`status: "warning"`, `auth: {"status": "unknown"}`, no slash commands.
`postPatch` used to flip the flag to `false` with `sed`
(`substituteInPlace` rejects the NUL bytes in `bin.mjs`), guarded by a
`grep -q` so an upstream change to the probe would have failed the build
loudly rather than silently stop patching it.

`nix/mcp.nix` is gone entirely now, and the box passes claude no
`--mcp-config` from anywhere, so there was nothing left for
`strictMcpConfig` to trip over — the flip bought nothing here any more.
Dropping it was verified against a rebuilt box the same way the flip's
absence would have shown up as a regression: `rm
~/.t3/caches/claudeAgent.json`, restart the unit, and the cache came back
`status: "ready"`, `auth.status: "authenticated"`. The underlying bug is
still real upstream — pingdotgg/t3code#5392 (2026-08-05) already reports
it — so nothing is owed from this box.

### Why nix builds it

`node-pty` compiles on aarch64-linux, so `python3` is in
`nativeBuildInputs`. Building in nix is what keeps the VM's runtime free
of a C and Python toolchain.

### Log noise that is not a failure

No `linux-arm64` resource-monitor binary ships, so t3 reports monitoring
unsupported. `Grok CLI health check failed` means no grok binary. `Failed
to flush telemetry` repeats every second when the endpoint is unreachable.

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

## AWS credentials ride `yo enter` through a host-side broker, never guest disk

AWS access is opt-in: set `YOLOBOX_AWS_PROFILE` to a host AWS CLI profile
before `yo enter`. v1 minted STS creds once, at enter time, and froze them
in the guest session's flat env (`AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`) — a session outliving the
STS duration (an hour, for this SSO) needed a fresh `yo enter`. v2 replaces
that with botocore's **container credential provider**: `start_aws_broker`
in `yo` starts `aws-broker` (repo root, `#!/usr/bin/env python3`, stdlib
only) on the Mac for that one pane, and the guest gets a loopback URL plus a
bearer token instead of the credentials themselves. The guest CLI calls back
to the broker for every request; the broker re-runs
`aws configure export-credentials --profile "$YOLOBOX_AWS_PROFILE" --format
process` shortly before the cached creds expire. An SSO re-auth on the Mac
(`aws sso login`) heals a running guest session with no re-enter — the
broker's next re-mint just succeeds again.

Flat env-var forwarding is gone from `yo` entirely, not merely unused:
botocore's credential provider chain ranks explicit `AWS_ACCESS_KEY_ID` env
vars above the container provider, so if `yo` still exported the frozen v1
vars alongside the broker's URL, they would win and silently defeat every
refresh — the guest would look wired up and would in fact be back to v1's
one-hour cliff. So the region resolution in `yo` now forwards only `AWS_REGION` (see
below); all credential minting and the no-long-lived-keys refusal moved
into `aws-broker`'s startup.

`aws-broker --profile P --watch-pid PID` runs `export-credentials`
synchronously at startup — this **is** the fail-fast validation, moved
here from v1's region resolution. A nonzero exit or a response with no
`SessionToken` (long-lived IAM user keys, which must never enter the guest
because they never expire) prints `ERROR=<one line>` to stdout and exits 1
before any socket opens. On success it binds `127.0.0.1:0` (kernel-assigned
port, so two concurrent `yo enter`s never collide on the host side),
mints a bearer token with `secrets.token_urlsafe(32)`, and prints exactly
`TOKEN=<t>\nPORT=<p>\n` — the only place a credential-adjacent value is
ever written, anywhere, including logs. Immediately after, it
`os.dup2`s `/dev/null` onto its own stdout, so nothing written later can
block a reader that only ever reads those two lines. `GET /creds` (with the
bearer token in `Authorization`, compared via `hmac.compare_digest`) serves
the cache when it has ≥10 minutes left, re-mints first (throttled to at
most one attempt per `--min-remint-interval`) when it doesn't, and keeps
serving a not-yet-expired cache across a failed re-mint — a real 500
`CredentialsNotAvailable` only happens once the cache is truly expired with
no fresh mint to replace it. `GET /health` (no auth) is for `yo aws-check`'s
guest-side forward probe.

`start_aws_broker` reads the broker's stdout as a pipe on its raw file
descriptor under a single 30 s deadline with `select`: a broker that dies
before handshaking fails `yo enter` loudly within 30s instead of hanging it
forever. The broker's own stderr goes to
`~/.local/state/yolobox/aws-broker/<pid>.log`, named by `os.getpid()` —
`yo`'s own pid — because `os.execvpe` at the end of enter replaces that
process in place rather than forking a child, so the pid the broker's
watch-pid thread polls (`kill -0` every 5s; gone → `os._exit(0)`) is the
exact pid that names the log file, with no second handshake field needed to
tell `yo` where to look. That same `os.execvpe` is what makes watch-pid
correct at all: an early exit after the broker starts (`check_herd_drift`'s exit 3,
for instance) leaves no orphan, because the watched pid disappears within
5s of `yo enter` itself exiting — there is no lingering parent shell to
keep it alive. A broker that somehow outlives its watched pid despite this
(pane killed hard enough that even `os._exit` housekeeping never runs) is
still bounded by `--idle-timeout` (4h default): no authenticated `/creds`
hit in that window and it exits on its own. That backstop is not currently
wired to anything log-visible beyond the broker's own stderr — a genuinely
orphaned broker is a leaked process, not a leaked credential, since its
bearer token dies with it and nothing else ever learns that token.

The forward itself is TCP `-R`, not the unix-socket forward the herd wiring
uses — a container-credentials URL has no `unix://` form botocore accepts
— and TCP `-R` has no `StreamLocalBindUnlink` equivalent to atomically
reclaim a stale guest-side listener. So claiming a guest port is
probe-and-retry: up to 5 candidates in `41000 + RANDOM % 1000`, each proved
with a throwaway `ssh -o ExitOnForwardFailure=yes -R <candidate>:...
true` before the real forward reuses that same candidate. The gap between
a successful probe and the real forward is an accepted, documented TOCTOU,
not a bug to fix — a collision in that window is vanishingly unlikely, and
a real one just fails the real forward exactly as a failed probe would
have. Once a port is claimed, `AWS_CONTAINER_CREDENTIALS_FULL_URI=http://
127.0.0.1:<guest-port>/creds` and `AWS_CONTAINER_AUTHORIZATION_TOKEN=<token>`
cross via `-o SendEnv=...` plus host-side exported env. Every forwarded
variable, herd and AWS alike, crosses by `SendEnv`, so no value ever appears
in ssh's argv. Nothing is written under guest `~/.aws`: the container credential
provider talks HTTP, not disk, and `AWS_CLI_SESSION_ID_DISABLED = "true"`
keeps the CLI's telemetry sqlite out of `~/.aws/cli/cache` too.

`yo ssh` and editor sessions (`yo code`, `yo zed`) deliberately carry no AWS
env or broker at all — same opt-in-and-silent posture as the herd wiring,
since most sessions have no need for AWS. `yo aws-check` is the doctor:
it proves the host prerequisites, starts a real throwaway broker and
forward (through `start_aws_broker` itself, not a reimplementation), then
from the guest curls `/health`, curls `/creds` and asserts all four keys
(`AccessKeyId`, `SecretAccessKey`, `Token` — not `SessionToken`, remapped —
and `Expiration`), then runs `aws sts get-caller-identity` and asserts a
role ARN comes back. It kills the throwaway broker on every exit path.

**Migration note.** v2 needs one guest `nixos-rebuild switch` before it
works — `nix/base.nix`'s `AcceptEnv` swapped the four flat `AWS_*` names for
`AWS_CONTAINER_CREDENTIALS_FULL_URI`/`AWS_CONTAINER_AUTHORIZATION_TOKEN`.
Until that switch lands, sshd silently drops both new vars (they are not
yet in its whitelist) and the guest CLI says "Unable to locate credentials"
with no error anywhere from `yo enter` — recognise it with
`sudo sshd -T | grep -i acceptenv` in the guest.

A DNS failure shape met on the very first v1 launch still applies unchanged:
credentials arrive but `aws sts get-caller-identity` dies with `Could not
connect to the endpoint URL: "https://sts.amazonaws.com/"`. That is DNS,
not AWS: this Mac's DNS filter sinkholes the *global* `sts.amazonaws.com`
name to 0.0.0.0, the guest inherits the Mac's resolver through lima's
forwarder (192.168.5.2), and the CLI only targets global endpoints when it
has no region. Regional endpoints (`sts.eu-west-1.amazonaws.com`) resolve
fine on both sides. So `yo` still forwards `AWS_REGION` alongside the
broker wiring — resolved from the profile's `region` key, falling back to
the host's `AWS_REGION` / `AWS_DEFAULT_REGION`, refusing loudly when
neither exists, because `export-credentials` never prints one and a
region-less guest CLI is broken for most services anyway.
