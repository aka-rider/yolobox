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
skill is linked into `~/.pi/agent/skills/`). Nothing VM-specific sits in
`~/.pi/agent/settings.json` — that file is a `~/.dotfiles` symlink and carries
the user's package list only.

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
