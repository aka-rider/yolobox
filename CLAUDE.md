# yolobox — notes for whoever works on this repo next

This repo describes one NixOS VM on a Mac, run by Lima, where coding agents
work with no host mounts and git as the only bridge. `README.md` gets a
person from nothing to a working project. This file holds the reasoning
behind the design and the shape of each failure we have met, so you can
recognise one instead of rediscovering it.

Read `CONSTITUTION.md` before changing anything. It is the short list of
rules this file justifies.

## Where things live

- `yo` — the Mac-side CLI. Everything the Mac does to the VM goes through
  it; `yo --help` describes each command.
- `flake.nix` — wires the modules together and threads the host username
  in.
- `nix/base.nix` — filesystems, boot, sshd, tmpfiles, git defaults, the
  lima units, core packages.
- `nix/podman.nix` — rootless podman with a docker-compatible socket.
- `nix/harnesses.nix`, `nix/agentic.nix` — the coding agents and their
  version overrides.
- `nix/mcp.nix` — MCP servers declared once, rendered per harness.
- `nix/herd-report.nix` — how in-VM agents report to the Mac's herd.
- `nix/display.nix` — the virtual X display, browsers, screen recording.
- `nix/lsp.nix` — the Python language-server wiring for every editor.
- `nix/t3.nix`, `nix/pkgs/t3.nix` — t3code built from npm and run as a
  service.
- `nix/pkgs/` — packages nixpkgs lacks or lags on.
- `lima/yolobox.yaml` — read once, when the instance is created.
- `templates/default` — `devbox.json` and `.envrc` for a new project.
- `homebrew/yolobox.rb` — the brew formula, with `@URL@`/`@SHA256@` holes;
  `release.yml` renders it into `aka-rider/homebrew-tap` on every tag.
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
`VERSION` at the repo root is the single source of truth for it, and the
brew and nix release for a given tag both read the same file, so the two
channels never drift from each other. The formula itself lives here too,
at `homebrew/yolobox.rb`: the release job renders it into the tap from a
`git archive` asset it uploaded itself, so the tap never holds a hand-edited
formula and the hash never rests on GitHub's non-stable auto-tarballs. `YOLOBOX_FLAKE` overrides the ref
for anyone running their own fork wholesale, e.g.
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
  unreleased clone has no version to pin a flake ref from — `flake_ref()`
  refuses outright rather than guess at a ref that may not exist yet, and
  says so on stderr, naming `YOLOBOX_FLAKE` as the way out. Recognise this
  by the exact wording of that refusal, not by a bare Nix fetch error.
- **The guest's own `yo` is inert.** The flake puts the `yolobox` package
  in the guest's `environment.systemPackages` so `yo --version` inside the
  VM can report which release built the box — that is the entire reason
  it is there. Every other subcommand needs `limactl` and `~/.lima`,
  neither of which exists in the guest, so `yo`'s `main()` carries an explicit
  `limactl` preflight guard that runs once per subcommand except `--help` and `--version`: expect a named, loud failure from any
  guest-side `yo` invocation beyond those two, never a bare `command not
  found`.
- **`nix flake check` throws with no explanation.** `flake.nix` reads
  `YOLOBOX_USERNAME` from the environment and `throw`s when it is unset,
  because it has no pure way to learn the host username otherwise (see
  "Two accounts: the operator mirrors the host, the agent does not" above). A bare `nix flake check`
  hits that throw immediately; it needs `--impure` with
  `YOLOBOX_USERNAME=$(id -un)` set, same as every `nixos-rebuild` in this
  repo.

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
never prunes old hashes. The fix belongs in that repo (see `TODO.md`).
Runners-up: 32 GiB of dead nix store paths, 4.7 GiB podman, 2.9 GiB
`/root/.cache/nix`, 2.5 GiB `~/.npm`.

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

The nixpkgs `playwright-mcp` wrapper sets `PLAYWRIGHT_BROWSERS_PATH`
unconditionally, so setting it in config is a no-op. It also exports
`PLAYWRIGHT_MCP_ISOLATED=1` unless `PLAYWRIGHT_MCP_USER_DATA_DIR` is set,
and passing both throws; the per-engine wrappers export the user-data-dir
before `exec`.

Upstream picks headless whenever `DISPLAY` is unset, silently. The wrapper
checks the X socket itself and refuses loudly instead.

A browser profile is named after the git toplevel relative to the guest
`$HOME`, slashes turned into `--`. A basename would merge `a/web` with
`b/web` and split sessions started from a subdirectory.

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
- Two same-project sessions on one engine share one browser: Chromium's
  singleton lock forwards the second launch (~8 s). Not isolation, not a
  failure.
- Chromium's storage flushes lazily; a session killed without
  `browser_close` can lose its last write.
- `yolobox-mcp-smoke` creates a stray profile named after its own cwd.
  Harmless.

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
not help. The policy channel is therefore unusable. The fix is a `claude`
wrapper on the VM's PATH that prepends `--settings
/etc/claude-code/herd-hooks.json`; that channel is always honoured and was
verified to survive the mid-session refresh.

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

A measurement trap that fooled this twice: `claude -p '<prompt>'` runs the
whole lifecycle in two seconds — `SessionStart`, `UserPromptSubmit`,
`Stop`, `SessionEnd` — and `SessionEnd` releases the agent from the herd.
So a headless run followed by a look at the herd shows nothing, exactly
like hooks that never ran. Use an interactive `claude`, read the log, or
watch the herd while the run is in flight. A leftover socket from a dead
`yo enter` answers `server_not_running` and is harmless;
`StreamLocalBindUnlink` lets the next `yo enter` rebind it.

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

Ownership: the flake owns `pi-mcp-adapter`, the herd-report extension and
the `mcp-scripting` skill. Skills, agents, prompts, `cc-compat` and
packages come from `~/.dotfiles/_do_install.sh`, re-run after a dotfiles
pull. Two extensions registering the same tool name kill pi at startup —
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

### Why dist/bin.mjs is patched

t3's Claude probe hardcodes `strictMcpConfig: true`, which Claude Code
refuses when an enterprise MCP config exists — and `nix/mcp.nix` renders
one at `/etc/claude-code/managed-mcp.json`. t3 swallows the failure
completely; nothing reaches journald. The evidence is
`~/.t3/caches/claudeAgent.json`: `status: "warning"`, `auth: {"status":
"unknown"}`, no slash commands. `postPatch` flips the flag to `false`. The
cache survives rebuilds — delete it, restart the unit, read it back.

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
forwarded port accepts and then resets, permanently. Unreported upstream
(see `TODO.md`).

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
that with botocore's **container credential provider**: `aws_broker_start`
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
one-hour cliff. So `aws_ssh_env_args` now forwards only `AWS_REGION` (see
below); all credential minting and the no-long-lived-keys refusal moved
into `aws-broker`'s startup.

`aws-broker --profile P --watch-pid PID` runs `export-credentials`
synchronously at startup — this **is** the fail-fast validation, moved
here from v1's `aws_ssh_env_args`. A nonzero exit or a response with no
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

`aws_broker_start` hands the broker's stdout to a fifo rather than a plain
pipe, so it can `read -t 30` each handshake line with a bound: a broker
that dies before handshaking fails `yo enter` loudly within 30s instead of
hanging it forever. The broker's own stderr goes to
`~/.local/state/yolobox/aws-broker/$$.log`, named by `$$` — `yo`'s own pid
— because `exec ssh` at the end of `cmd_enter` replaces that process
in place rather than forking a child, so the pid the broker's watch-pid
thread polls (`kill -0` every 5s; gone → `os._exit(0)`) is the exact pid
that names the log file, with no second handshake field needed to tell
`yo` where to look. That same `exec` is what makes watch-pid correct at
all: an early exit after the broker starts (`herd_drift_check`'s exit 3,
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
cross via `-o SendEnv=...` plus host-side exported env — not `SetEnv`,
which the herd wiring uses for its non-secret vars: `SetEnv` embeds values
in ssh's argv, where `ps` shows them to every process on the Mac for the
session's lifetime, while `SendEnv` puts only the *names* in argv and the
guest's `AcceptEnv` whitelist (`nix/base.nix`) accepts both mechanisms
alike. Nothing is written under guest `~/.aws`: the container credential
provider talks HTTP, not disk, and `AWS_CLI_SESSION_ID_DISABLED = "true"`
keeps the CLI's telemetry sqlite out of `~/.aws/cli/cache` too.

`yo ssh` and editor sessions (`yo code`, `yo zed`) deliberately carry no AWS
env or broker at all — same opt-in-and-silent posture as the herd wiring,
since most sessions have no need for AWS. `yo aws-check` is the doctor:
it proves the host prerequisites, starts a real throwaway broker and
forward (through `aws_broker_start` itself, not a reimplementation), then
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
