# yolobox — operating notes

README is the hit-the-ground guide. This file holds the reasoning behind the
steps and the failure modes worth recognising.

## The guest account mirrors the host

There is no fixed "yolobox account". Lima's cidata provisions a guest
account named and uid-matched after whichever Mac account started the VM
(`lima/yolobox.yaml`'s `user: false` does not disable this — it only turns
off a different lima default). `flake.nix` declares that same account
declaratively (`users.users.${username}` in every `nix/*.nix` module that
needs it) rather than inventing a separate one, because Nix has no pure way
to learn the host username: `YOLOBOX_USERNAME=$(id -un)` plus `--impure` on
every `nixos-rebuild` invocation is how it gets in (`yo t3`'s switch hint
shows the exact form; `yo`'s own `VM_USER_HOME` is computed the same way).

An earlier revision hardcoded this account as `xiii` — a leftover from a
previous Mac account of that name. Once the Mac account changed to
something else, `xiii` silently kept existing as a second, colliding
identity: same uid as the real account (nixos-rebuild pins it there on
purpose, for podman/virtiofs uid compatibility), but a different
`/home/*.guest` directory. Every nix module that stores state under
`config.users.users.xiii.home` (mcp, lsp, herd-report, display, t3) was
writing into that second, never-interactively-used home instead of the
real one — invisible to `getpwuid`-based lookups (`whoami`, `sudo`'s
`SUDO_USER`) too, since those resolve ambiguously to whichever of the two
same-uid passwd entries comes first. `nixos-rebuild switch` removes a
previously-declared user once its module stops declaring it (`mutableUsers
= true` only stops nix from touching accounts it never declared), so
retiring `xiii` from every module in one switch was enough to clear the
collision — no manual `userdel` needed.

## SSH identities: the forwarded agent, and the two GitHub accounts

`ForwardAgent yes` forwards `$SSH_AUTH_SOCK`, **not** `IdentityAgent`. On this
Mac those are two different agents: `$SSH_AUTH_SOCK` is Apple's launchd agent,
which holds no identities, while every real key lives in 1Password. So `yes`
forwarded an empty agent — `ssh-add -l` in the box answered "Could not open a
connection to your authentication agent", because sshd sets `SSH_AUTH_SOCK`
only once the forward carries something. `ssh_args` in `yo` therefore gives
`ForwardAgent` the 1Password socket path itself.

Because the forward is decided by whichever session opens lima's shared
`ControlMaster`, a change here is invisible until the old master goes:
`ssh -F ~/.lima/yolobox/ssh.config -O exit lima-yolobox`.

Two GitHub accounts (`github@iurii.net` and the work `iurii-tech`) share one
`github.com`, so the account is chosen by *which key* is offered. The host
does that with a `Host github-iurii-tech` alias whose `IdentityFile` names a
**public** key file — that is the agent-key selector, not a key — and
`~/.dotfiles/gitconfig` rewrites `git@github.com:brocc-ab/` to that alias.
`yo seed` copies the Mac's `~/.ssh/config` and every `~/.ssh/*.pub` in, minus
two host-only lines: lima's own `Include`, and `IdentityAgent` — the box must
use the forwarded socket, and a copied `IdentityAgent` would point at a path
that does not exist there and take the agent away. Still no private key in the
box.

`yo seed` also carries the directory-level git identities,
`~/Developer/<group>/.gitconfig`, into `~/wrk/<group>/.gitconfig`. Those files
sit *next to* a group of repos rather than inside one, so no `yo link` push
ever moves them, and without them the `includeIf` in `~/.dotfiles/gitconfig`
resolves to nothing and work repos silently commit under the personal email.
For the same reason that include's `path` must be spelled `~/wrk/...` for the
`gitdir:~/wrk/` case: on the Mac `~/wrk` is a symlink to `~/Developer`, but in
the box `~/wrk` is the real directory and `~/Developer` does not exist.

How to recognise the whole class of failure: `ssh -T git@github.com` and
`ssh -T git@github-iurii-tech` inside the box must greet two *different*
account names. "Could not resolve hostname github-iurii-tech" means the ssh
config never arrived; a greeting with the wrong account name means the pub
keys did not.

## Per-project dev shells

### Why a project must be a git repo

For one reason only: the push channel. `./yo link` git-inits the VM
side, so linked projects are correct by construction; a project created
directly in the VM needs `git init` first.

The flake-evaluation reasons that used to apply — `git+file:` sees tracked
files only, and `path:` copies a directory verbatim, dying with
`file '...' has an unsupported type` on a unix socket under the source
root — now concern only the yolobox repo itself. `~/wrk/yolobox` is still a
flake (`nixosConfigurations` only), so there and only there the old
corollary holds: untracked files do not exist as far as Nix is concerned,
and a file git has never heard of makes evaluation fail as though it were
absent from the store copy. `git add` before `nixos-rebuild`.

### Authoring can happen on either side

The VM's project checkout is a push-to-checkout target, which refuses a push
when the worktree **or the index** differs from HEAD. This is not a property
of linked repos: `nix/base.nix` sets `receive.denyCurrentBranch = "updateInstead"`
in the VM's `/etc/gitconfig`, so it holds for every repo in the VM, including
one you `git init` by hand. `cmd_link` sets it repo-locally too, redundantly.
Fetch is unrestricted; only push is gated.

devbox never touches the git index. The flake-era hazard — `direnv allow`
ran `git add --intent-to-add flake.lock`, which dirtied the index and broke
the push channel for good — is gone, so authoring `devbox.json` VM-side is
as safe as host-side. The remaining discipline is ordinary git: the VM
commits `devbox.lock` alongside its work on the checked-out branch, so pull
the VM's commits before pushing again, or the push lands on a diverged
checkout and is refused like any other.

### devbox specifics

- `devbox.lock` is committed to each project. VM-side agents commit it with
  their work; the host pulls it back.
- Resolution needs network on the first add: package metadata from
  search.devbox.sh, store paths from cache.nixos.org.
- `.devbox/` is per-machine state and is gitignored per project.
- direnv auto-trusts every `.envrc` under `~/wrk` (the guest account's own
  home — see "The guest account mirrors the host" below), so no
  `direnv allow` step exists. The whitelist lives in `/etc/direnv/direnv.toml`,
  rendered by `programs.direnv.settings`. The NixOS direnv module exports
  `DIRENV_CONFIG=/etc/direnv`, which overrides the XDG location — a
  `~/.config/direnv/direnv.toml` is silently ignored, so never put the
  whitelist there. Being an `/etc` symlink, it flips on `nixos-rebuild
  switch`; no tmpfiles or reboot step applies.

## tmpfiles rules apply on `switch` via resetup

The VM carries `systemd-tmpfiles-resetup.service`, upstream's switch-time
twin of the boot-only setup unit, so a `switch` that adds a new tmpfiles rule
(verified with the `~/artifacts` directory rule) takes effect immediately —
no reboot needed. `sudo systemd-tmpfiles --create` still re-runs every rule
system-wide, including force-replacing `L+` links, and resetup does the same
on every `switch` — relied upon for the `L+` links that fill pi's
auto-discovery directories (`nix/herd-report.nix`).

## Browsers and the virtual display

The nixpkgs `playwright-mcp` wrapper `--set`s `PLAYWRIGHT_BROWSERS_PATH` to
the full browsers bundle unconditionally, so setting that variable at the
config level is a no-op — this supersedes the old Gotcha 9 note that used to
live in `nix/agentic.nix`. The wrapper also exports `PLAYWRIGHT_MCP_ISOLATED=1`
unless `PLAYWRIGHT_MCP_USER_DATA_DIR` is set, and passing both throws; the
per-engine wrapper scripts export the user-data-dir env before `exec` so
isolated mode never turns on.

Upstream's headed/headless choice is `headless = linux && !DISPLAY`: a
missing `DISPLAY` silently degrades to headless instead of failing. The
wrapper checks the X socket itself and refuses loudly if the display isn't
up, rather than falling through to a silent headless run.

Project identity for a browser profile is the git toplevel path relative to
`~/wrk`, slashes turned into `--` — not `basename($PWD)`. Basename would
merge unrelated `a/web` and `b/web` into one profile (cookie bleed across
projects) and would split subdirectory-started sessions of the same repo
into separate profiles.

`openbox` readiness is an `ExecStartPre` that waits on the X socket, not a
`Restart=` crash loop: five restarts in ten seconds trips systemd's default
start-limit and leaves the unit permanently failed.

Use `pkgs.xvfb`, not the deprecated `pkgs.xorg.xvfb` alias. Its
`meta.mainProgram` is wrongly set to `"X"`, so `lib.getExe` on it resolves to
a binary that doesn't exist — spell out `${pkgs.xvfb}/bin/Xvfb` directly.

No project-local tmpfiles rule for `/tmp/.X11-unix`: systemd's own `x11.conf`
already ships one, and a duplicate just logs errors every boot.

The `yolobox-screen-record` pidfile lives under `~/.local/state/yolobox`, not
`/run/user/$UID`: `/run/user/$UID` is destroyed at last-session logout, but
the backgrounded `ffmpeg` survives it (NixOS sets `KillUserProcesses=false`),
which would orphan a recording no one can finalize. `SIGINT` finalizes the
mp4; `SIGKILL` corrupts it.

Fonts matter now: headless rendering worked with no `fonts.packages` at all;
headed screenshots without fonts render as tofu.

`yolobox-mcp-smoke` spawns the MCP servers with env rendered from its own
cwd, so it creates a stray profile and artifacts directory named after that
cwd. Harmless, just a smoke-test byproduct.

Same-engine, same-project concurrency (two sessions racing the one
persistent profile) produces no lock error and no hang: Chromium's singleton
lock forwards the second launch to the running instance (~8s), so concurrent
same-project sessions silently share one browser. Not isolation, but not a
failure either.

Chromium launches and navigates sandboxed (no `--no-sandbox`) as the
non-root guest account; Firefox likewise needed no dbus workaround.

Profiles persist per project, but Chromium's LevelDB-backed storage flushes
lazily — a session killed without `browser_close` can lose its last write
(verified: a write two generations back survived, the latest didn't). End
sessions with a clean `browser_close` before shutdown.

Default nixpkgs ffmpeg has no x11grab (built with
`--disable-xlib`/`--disable-libxcb*`) — that's why `nix/display.nix` uses
`ffmpeg-full`.

Native browser video is `browser_start_video` / `browser_stop_video` (gated
by `--caps devtools`), verified producing VP8 webm in
`~/artifacts/<project>/`.

## Herd reporting: recognising a silent failure

Only two of the in-box harnesses report into the herd: **claude** (via a
hook map) and **pi** (via a bundled herd-report extension) — see
`nix/herd-report.nix`. opencode and codex lost theirs when `nix/herd.nix`
was deleted, and crush has no upstream herdr integration. An unreported
agent still runs fine; it shows as `unknown` rather than `yolobox:<agent>`.

pi's extension reaches pi through auto-discovery: `nix/herd-report.nix` links
it into `~/.pi/agent/extensions/`, next to the pi-mcp-adapter package (its one
skill is linked into `~/.pi/agent/skills/`).

A herdr-installed `~/.pi/agent/extensions/herdr-agent-state.ts` (written by
herdr's own integration installer) silently knocks pi out of the herd: it
reports under source `herdr:pi`, which the server reserves for its own
screen/session detection and clears for a boxed agent. A tmpfiles `r` rule
in `nix/herd-report.nix` deletes it on every boot and switch.

Herd wiring rides `./yo enter` only — the forwarded socket and the herd
env come from `cmd_enter`. `./yo ssh` deliberately carries neither. `yo code`
and `yo zed` sessions carry no herd env either — agents started from editor
terminals report as `unknown`; expected, not a failure. t3-spawned claude is
a partial exception now (see the wrapper below): it carries the hooks, since
t3 execs `claude` from `/run/current-system/sw` and picks up the wrapper like
any other caller, but it still has no `HERDR_PANE_ID` or forwarded socket
unless something arranges one, so it still cannot actually report — it just
now fails for the *expected* reason (no herd env) instead of the old one (no
hooks).

The forwarded herdr socket lives at `/run/yolobox/herd-host.<pane>.sock`; the
tmpfiles rule in `nix/base.nix` explains why it is not under `$HOME`.

Every in-box claude session was invisible in the host herd until two
independent defects were found and fixed, and the second one overturns an
invariant this section used to state as fact — read it before trusting
anything the log does or doesn't show.

**Defect 1 — herdr protocol drift.** The Mac's Homebrew herdr self-updated to
0.8.2 (wire protocol 20) while the box stayed pinned at 0.8.0 (protocol 19).
Every report was rejected with `protocol_mismatch`. The sharp edge: the
protocol moved across a *patch* bump, so version equality — not
major.minor equality — is the only honest cheap proxy for compatibility.
The pin was a hand-copied duplicate of a fact that lives on the host and
updates itself behind Homebrew, so drift was the design, not an accident.
Fixed structurally: `yolobox.harness.herdr.version`/`hash` now default to
null so the box tracks nixpkgs' `pkgs.herdr`, and the version/hash option
remains only as an escape hatch for when nixpkgs lags a herdr release. The
invariant to hold is: **the box's herdr version must equal the Mac's
Homebrew herdr version.** `yo enter` refuses on drift (exit 3, overridable
with `YOLOBOX_SKIP_HERD_CHECK=1` — load-bearing, because during a nixpkgs
lag there may be no remedy at all and `yo enter` is the one command with no
other workaround); `yo status` reports the drift without failing;
`yo herd-check` is the authoritative end-to-end proof.

**Defect 2 — claude never ran the hooks at all, and this is the one worth
remembering.** The hook map in `/etc/claude-code/managed-settings.json` was
always correct — the byte-identical file passed as `--settings` fires every
event. But Claude Code 2.1.220 assembles `policySettings` from three tiers —
remote server-fetched managed settings, MDM, then
`/etc/claude-code/managed-settings.json` — and takes **only the first
non-empty tier, with no merge**. This account's `~/.claude/remote-settings.json`
holds `{"channelsEnabled": true}`; that single flag makes the remote tier
non-empty, so it becomes the entire `policySettings` object and the `/etc`
file is discarded wholesale. Nothing warns, nothing logs. Proven causally:
emptying that cache to `{}` made the hooks fire immediately and created the
rejection log for the first time ever; restoring the flag stopped them
again. Worse, the remote payload is re-fetched and re-applied roughly 180ms
into every session (`Programmatic settings change notification for
policySettings` in `--debug` output), so it clobbers policySettings
mid-session too — even an initially-empty cache would not have saved it.
The policy channel is therefore unusable for this, permanently.

The fix is the `flagSettings` channel: the same hook map is rendered to
`/etc/claude-code/herd-hooks.json` and delivered by a `claude` wrapper on the
box's PATH that prepends `--settings`. claude adds `flagSettings` to its
allowed sources unconditionally, and those hooks were verified to survive
the mid-session remote refresh.

**The invariant this section used to state, and why it was false.** It used
to say that a rejected report "does get logged, to
`~/.local/state/yolobox/herd-report.log`." That log's absence proves
nothing: a hook that never runs writes nothing, and the socket guard exits 0
before the log path is even reached — exactly defect 2's failure mode, and
it produced total silence, not a rejection entry. The log file had never
been created in any session, going back weeks. The host's herdr server log
does not record `protocol_mismatch` rejections either, so its silence is not
evidence in the other direction.

When an agent shows nothing in the herd, work down this ladder rather than
guessing:

1. `ls -l /run/yolobox/` — is the forwarded socket there at all. No socket
   means the `-R` forward failed to bind, and the ssh client said so at
   connect time (`Warning: remote port forwarding failed for listen path
   ...`) — usually `/run/yolobox` missing after a `switch` that was never
   followed by a reboot.
2. `sudo strace -f -qq -e trace=execve -p <claude pid>` grepped for
   `herd-report` — do the hooks actually run at all. `sudo` is required
   because `kernel.yama.ptrace_scope=1` blocks an unprivileged tracer.
3. `strace -e trace=openat claude mcp list | grep managed-settings` — is the
   file even read. This is already answered yes, so don't re-derive it: the
   file being read tells you nothing, because it is read and then discarded
   whole by the tier collapse in defect 2.
4. `claude --debug` and grep for `Hook output` / `policySettings` — the
   authoritative view of which channel actually won.

A grep gotcha that cost real minutes, worth recording: filtering
`/proc/<pid>/environ` for the herd variables needs
`grep -E '^(YOLOBOX_HERD|HERDR_)'`. Writing `'^(HOME|HERDR_|PATH)='` silently
matches nothing, because the `=` binds after the alternation — it searches
for a variable literally named `HERDR_`. It briefly looked like claude was
scrubbing the env; it was not.

The reporter's always-exit-0 contract now has one deliberate exception:
`SessionStart` routes to a `start` state token which, on a failed report,
writes the diagnostic to stderr as well as the log and exits 1. That is safe
because SessionStart is off the work path — it runs once, before any tool
call, and cannot interrupt anything in flight — and Claude Code treats a
nonzero-but-not-2 hook exit as a non-blocking error whose stderr surfaces to
the user. The other five events keep exit-0 verbatim: `PreToolUse` fires on
every tool call and would otherwise turn one broken socket into a wall of
warnings. The log line now carries the guest herdr version and socket path
so a rejection is self-describing without cross-referencing nix files.

`yo herd-check` proves the whole chain end to end — host server health,
version parity, the forwarded socket, a reporter round trip, a release round
trip, that the hook map's commands exist, and that claude actually executes
them. Run it from a herdr pane with no agent on it: it reports and then
releases that pane, so a live agent's pane is not the one to use.

Do not add age-based tmpfiles cleanup for stale sockets. `connect(2)` on a
pathname socket bumps atime (`unix_find_bsd()` → `touch_atime()`), but
`relatime` caps the refresh at roughly 24h and traffic on an established
connection touches nothing — so an `age` would reap a live pane's socket and
silently kill its reporting. tmpfs makes the question moot anyway.

## pi's settings.json belongs to pi

`~/.pi/agent/settings.json` is pi's own writable file. pi rewrites it on its
own (theme, model, `lastChangelogVersion`, and every `pi install`), and its
write path logs the error instead of raising it. So neither nix nor
`~/.dotfiles` may link it — `~/.dotfiles/_do_install.sh` deliberately never
links it either.

A deleted `nix/pi.nix` module (commit `cb5e863`) used to render
`/etc/yolobox/pi/settings.json` and seed the home copy. The module went, the
seeded symlink stayed: `~/.pi/agent/settings.json ->
/etc/static/yolobox/pi/settings.json` dangled in the VM home until it was
removed by hand.

Consequence, while it dangled: `settings.json` is the only place pi keeps its
`packages` list, so nothing pinned in `~/.dotfiles/pi/packages.json`
installed. `pi list` printed "No packages installed",
`~/.pi/agent/npm/node_modules` did not exist, and `cc-compat` printed
"AskUserQuestion is unavailable — cannot load
@juicesharp/rpiv-ask-user-question@not installed" on every start.

How to recognise it: `ls -l ~/.pi/agent/settings.json`. A symlink there is
the defect. Fix is `rm` — pi recreates the file on its next write. No
tmpfiles rule can do this instead: there is no "delete only if dangling"
rule, and an unconditional `r` would delete the real settings file pi
creates in its place.

Apply the pinned list with pi's own command, `pi install <source>` per
entry — never by writing pi's settings file by hand.

The box carries no C or Python toolchain, so an extension whose dependency
tree holds a native module without an aarch64 prebuild cannot install: npm
falls back to `node-gyp`, which finds no `python3`, `gcc` or `make`. This
dropped `@plannotator/pi-extension` from the pinned list — it pulls
`node-pty`, whose 1.1.0 release ships prebuilds for darwin and win32 only,
so on aarch64-linux it always falls through to a compile.
Prebuilt native packages are fine: `@ast-grep/napi` and its `ast-grep` binary
(pi-lens, pi-simplify) load and run as glibc builds.

Division of ownership worth stating once: the flake owns only
`pi-mcp-adapter`, the herd-report extension, and the `mcp-scripting` skill.
The user layer — skills, agents, prompts, `cc-compat`, packages — comes from
`~/.dotfiles/_do_install.sh`, which must be re-run inside the VM after a
dotfiles pull.

Two extensions that register the same tool name kill pi at startup: it
refuses to load and names both files. This retired the flake's own pi-lsp
extension, which collided with pi-lens (`lsp_diagnostics`) from the user
layer — pi-lens covers basedpyright off the same devbox PATH plus every
other language, so pi's LSP now belongs to the user layer entirely. A
retired `L+` link is not removed by the rebuild that drops its rule: delete
`~/.pi/agent/extensions/<name>` and any stale config by hand, or pi keeps
loading the previous closure.

## t3: a nix-built npm CLI, run as a service

`nix/pkgs/t3.nix` builds the npm package `t3` (t3code) from its registry
tarball; `nix/t3.nix` puts it in `environment.systemPackages` and runs
`t3 serve --host 127.0.0.1 --port 3773` as `systemd.services.t3` under
the guest account. Loopback inside the guest — but not private to the Mac:
`lima/yolobox.yaml` declares a `guestPort: 3773` forward with
`hostIP: "0.0.0.0"`, and lima's forwarder dials the guest's loopback from
*inside* the guest, so the port is published on every host interface without
the guest firewall (NixOS default-deny, only 22 open) ever being traversed.

But that forward is inert on a VM that already exists. Lima materialises the
instance config at *creation* time into `~/.lima/yolobox/lima.yaml`, and
`yo up` on an existing instance runs `limactl start --yes yolobox`, which
reads that copy; `cmd_up` names `lima/yolobox.yaml` only in the branch that
creates a missing instance. So the repo file is authoritative exactly once,
and everything after that is drift — verified here: the 3773 rule was in the
repo file and absent from `~/.lima/yolobox/lima.yaml`. Migrating an existing
VM means stopping it (`limactl edit` on a running instance is a hard
`cannot edit a running instance`) and restating the whole array:

```sh
limactl stop yolobox
limactl edit yolobox --set '.portForwards = [{"guestPort":3773,"hostIP":"0.0.0.0"},{"proto":"udp","guestPort":68,"guestIP":"0.0.0.0","ignore":true}]'
./yo up
```

The udp/68 rule is repeated there because `--set` is yq v4 and lima
deliberately disables yq's `load`/`load_str`/`env` operators (see
`limactl help yq-restrictions`), so the expression cannot read
`lima/yolobox.yaml` and merge from it. `yo` does not paper over the drift on
purpose: syncing would need a YAML parser on the host, and only Homebrew's
python3 carries one here — the system python3 does not.

`t3 serve`'s `$HOME` must be the exact same `$HOME` an interactive SSH
session gets — `t3 pair` (run interactively to mint the web UI's pairing
token) discovers the running server by looking for
`$HOME/.t3/{userdata,dev}/server-runtime.json`, so a mismatch here makes
`yo t3` fail with "No running T3 Code server found" even though the
service is active. This is exactly the failure mode the guest-account
mirroring below exists to prevent.

The service gets `HOME` and a `path` entry for `/run/current-system/sw`,
because a system unit inherits neither a login PATH nor a home, and t3
shells out to the harnesses it drives (claude, opencode) and to git.
Use the `path` option, not `environment.PATH`: NixOS already defines
`environment.PATH` for every service, so a second definition is an
option conflict, not an override.

### Why the package.json must be patched

The published `package.json` carries the t3code monorepo's pnpm-style
`overrides`, whose keys use the `parent>child` form
(`"@clerk/clerk-js>@base-org/account": "-"`). npm accepts those keys in a
workspace root but validates them as package names once this package is the
install root, so any npm command dies with `EINVALIDPACKAGENAME`. Thus
`postPatch` deletes the field with `jq` before npm sees it. It calls jq by
store path rather than through `nativeBuildInputs`, because the same
`postPatch` also runs inside the npm-deps fixed-output derivation, which
does not inherit build inputs — a jq from `nativeBuildInputs` gives
`jq: command not found` there and nowhere else.

The vendored `nix/pkgs/t3-package-lock.json` must be generated from the
*stripped* package.json, or the same rejection happens at lock time. The
version-bump ritual is therefore three hashes in order: fetch the new
tarball and pin its `hash`; unpack it, `jq 'del(.overrides)'`, run
`npm install --package-lock-only --ignore-scripts`, and vendor the result;
then build once with a wrong `npmDepsHash` and pin the hash nix reports.

### Why dist/bin.mjs must be patched too

t3's Claude capability probe hardcodes `strictMcpConfig: true`. Claude Code
refuses `--strict-mcp-config` whenever an enterprise MCP config is present,
and `nix/mcp.nix` renders exactly that at `/etc/claude-code/managed-mcp.json`.
So the probe exits 1 within ~150ms with "You cannot use --strict-mcp-config
when an enterprise MCP config is present".

Recognising it is the hard part, because t3 hides the failure completely: the
probe options set `stderr: () => {}` and the call site is wrapped in
`Effect.orElseSucceed(() => void 0)`. Nothing reaches journald — `journalctl -u
t3 | grep -c claude` returns 0 even while this is happening. The evidence lives
in `~/.t3/caches/claudeAgent.json` instead: `status: "warning"`, `auth:
{"status": "unknown"}`, no slash commands, and the message "Could not verify
Claude authentication status from initialization result."

The `postPatch` flips the flag to `false`. The cost is that each probe now
starts the three servers in `managed-mcp.json` instead of being isolated from
them; they are short-lived node processes, and playwright starts no browser
until a tool call, so the probe still finishes well inside its 25s timeout.

That cache is not invalidated by a rebuild. To re-test, delete
`~/.t3/caches/claudeAgent.json`, restart the unit and read the file back — do
not go looking in journald.

### Why nix builds it at all

`node-pty@1.1.0` ships prebuilds for darwin and win32 only, and its install
script is `node scripts/prebuild.js || node-gyp rebuild`, so on aarch64-linux
it compiles. `python3` is in `nativeBuildInputs` for exactly that. Building in
nix is what keeps the box's runtime free of a C and Python toolchain — the
same absence that blocks `pi install` of native extensions.

### Log noise that is not a failure

No `linux-arm64` `dist/resource-monitor` binary ships upstream (darwin-arm64,
darwin-x64, linux-x64, win32-x64 only), so t3 reports resource monitoring as
unsupported and carries on.

`Grok CLI health check failed { errorTag: 'PlatformError' }` at startup only
means the grok binary is not in the box. Same for any other provider CLI the
box does not carry.

`Failed to flush telemetry ... HttpClientError` repeats every second when t3
cannot reach its telemetry endpoint. It does not stop the server.

### The web UI needs a pairing token

A bookmarked bare URL fails auth. `t3 serve` prints a Pairing URL at startup —
read it from `journalctl -u t3` — or mint a fresh one from an ssh session with
`t3 pair`, which is why the package is in `systemPackages` and not only in the
unit.

### The pairing URL's host is whatever `--host` the server was started with

t3 computes it as `resolveDirectPairingBaseUrl = (state) => state.devUrl ??
resolveHeadlessConnectionString(state.host, state.port)`. The unit starts it
with `--host 127.0.0.1`, so every URL `t3 pair` mints is loopback-only and
useless on another machine — and `t3 pair` has no override for it. Only
`t3 auth pairing create --base-url` takes one. That is why `yo pair` is a
separate command rather than a flag on `yo t3`: it goes through
`auth pairing create`, defaulting the base URL to
`http://$(scutil --get LocalHostName).local:3773`, and prints the result
instead of opening it locally.

### Pairing runs client→server only; two boxes meet in one browser

There is no operation that pairs two t3 servers with each other, so do not go
looking for one. t3's own bundle documents `t3 auth pairing create` as "Issue
a new *client* pairing token", redeeming a token yields a browser session, and
the paired device type is one of `["desktop","mobile","tablet","bot"]`.
Multi-machine use is a *client-side* list instead: the web UI carries the
string `Click "Add environment" to pair another environment`, and the docs
say "Every saved environment is offered, not only the local one". Thus the way
to drive two VMs is one browser holding both environments — `yo pair` on each
box, both URLs redeemed in the same browser.

### `lsof` shows lima listening on `*:80` and `*:443` — pseudoloopback, not exposure

`lsof -nP -iTCP -sTCP:LISTEN -a -c limactl` shows lima bound to the host
wildcard on 80 and 443 while every other forwarded port sits on `127.0.0.1`.
It looks like a hole and is not. From lima's `pkg/portfwd/listener_darwin.go`:
when `hostIP == 127.0.0.1 && hostPort < 1024`, lima binds `0.0.0.0:<port>`
instead, because on macOS a non-root process cannot bind `127.0.0.1:80` but
*can* bind `0.0.0.0:80`. The listener it returns is a
`pseudoLoopbackListener`, whose `Accept()` closes any connection whose peer
is not loopback — so a LAN client completes the TCP handshake and is then
dropped. The `<1024` boundary is why guest 8443 lands on `127.0.0.1:8443`
while 80 and 443 do not.

Deliberately left alone: an explicit `hostIP: "127.0.0.1"` would be a no-op,
since that is already the resolved value and is exactly what triggers the
branch; and remapping to unprivileged host ports would move the project's
caddy off `https://localhost` for no security gain. The real cost is worth
naming, though: lima occupies host ports 80 and 443 on every interface, so
nothing else on the Mac can bind them.
