# yolobox — operating notes

README is the hit-the-ground guide. This file holds the reasoning behind the
steps and the failure modes worth recognising.

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
- direnv auto-trusts every `.envrc` under `/home/xiii.guest/wrk`, so no
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

Chromium launches and navigates sandboxed (no `--no-sandbox`) as non-root
`xiii`; Firefox likewise needed no dbus workaround.

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
managed-settings.json hook map) and **pi** (via a bundled herd-report
extension) — see `nix/herd-report.nix`. opencode and codex lost theirs when
`nix/herd.nix` was deleted, and crush has no upstream herdr integration. An
unreported agent still runs fine; it shows as `unknown` rather than
`yolobox:<agent>`.

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
terminals report as `unknown`; expected, not a failure.

The forwarded herdr socket lives at `/run/yolobox/herd-host.<pane>.sock`; the
tmpfiles rule in `nix/base.nix` explains why it is not under `$HOME`.

When an agent shows nothing in the herd, `ls -l /run/yolobox/` first. No socket
means the `-R` forward failed to bind, and the ssh client said so at connect
time (`Warning: remote port forwarding failed for listen path ...`) — usually
`/run/yolobox` missing after a `switch` that was never followed by a reboot.
The failure is otherwise invisible: `nix/herd-report.nix` guards on
`[ -S "$HERDR_SOCKET_PATH" ]` and exits 0 before it can log anything.

If the socket *is* there and reports still vanish, the host herdr server is
rejecting them — usually a host/guest protocol mismatch. Those rejections do
get logged, to `~/.local/state/yolobox/herd-report.log`. Compare `herdr
status`'s `compatible:` line on the host against the guest's pinned
`yolobox.harness.herdr.version`/`hash` in `nix/harnesses.nix`.

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
`xiii`. Loopback only — reach it through the ssh forward, not from outside
the box.

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
