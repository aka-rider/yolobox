# yolobox — operating notes

README is the hit-the-ground guide. This file holds the reasoning behind the
steps and the failure modes worth recognising.

## Per-project dev shells

### Why a project must be a git repo

For one reason only: the push channel. `./yolobox2 link` git-inits the VM
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

## tmpfiles rules do not take effect on `switch`

`nixos-rebuild switch` does not re-run `systemd-tmpfiles-setup.service` — it is
boot-only and refuses a manual restart. A **new** rule of any type does not
take effect on `switch` alone; this covers both `"C"` copy-if-absent rules
(such as the one seeding `~/.pi/agent/settings.json`) and the `d /run/yolobox`
directory rule the herdr forward depends on. Pick it up with a real reboot
(`limactl restart yolobox`) or `sudo systemd-tmpfiles --create` in the VM. Note
the latter re-runs *every* rule system-wide, including force-replacing `L+`
rules.

## Herd reporting: recognising a silent failure

Only two of the in-box harnesses report into the herd: **claude** (via a
managed-settings.json hook map) and **pi** (via a bundled herd-report
extension) — see `nix/herd-report.nix`. opencode and codex lost theirs when
`nix/herd.nix` was deleted, and crush has no upstream herdr integration. An
unreported agent still runs fine; it shows as `unknown` rather than
`yolobox:<agent>`.

Herd wiring rides `./yolobox2 enter` only — the forwarded socket and the herd
env come from `cmd_enter`. `./yolobox2 ssh` deliberately carries neither.

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
