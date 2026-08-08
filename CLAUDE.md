# yolobox — operating notes

README is the hit-the-ground guide. This file holds the reasoning behind the
steps and the failure modes worth recognising.

## Per-project dev shells

### Why the project must be a git repo

Nix reads a flake directory one of two ways. Inside a git repo it uses
`git+file:`, which sees **tracked files only**. Outside one it degrades to
`path:`, which copies the directory verbatim — `.git`, build trees, and unix
sockets included. `path:` on a directory containing a socket dies with
`file '...' has an unsupported type`.

That is not hypothetical: it is what happens if a flake lands in `$HOME`.
`./yolobox2 link` git-inits the VM side, so linked projects are correct by
construction; a project created directly in the VM needs `git init` first.

Corollary: untracked files do not exist as far as Nix is concerned. A
`flake.nix` git has never heard of makes evaluation fail as though the file
were absent from the store copy. Hence the explicit `git add` on both sides.

### Why authoring happens on the host

The VM's project checkout is a push-to-checkout target, which refuses a push
when the worktree **or the index** differs from HEAD. This is not a property
of linked repos: `nix/base.nix` sets `receive.denyCurrentBranch = "updateInstead"`
in the VM's `/etc/gitconfig`, so it holds for every repo in the VM, including
one you `git init` by hand. `cmd_link` sets it repo-locally too, redundantly.
Both `nix flake init` and `direnv allow` run `git add --intent-to-add` — on
the files they create, and on `flake.lock` respectively — so authoring the
flake VM-side dirties the index and breaks the `link` push channel for good.
Fetch is unrestricted; only push is gated.

`flake.lock` can only be produced VM-side, so the loop is standing, not
one-off: host authors, VM locks and commits, host pulls back. Anything that
regenerates the lock — notably `nix flake update yolobox` — goes round it
again. Leaving the lock uncommitted in the VM is what blocks the next push.

Copying `templates/default/` beats `nix flake init`: the host has no Nix by
design, and copying sidesteps `nix flake init`'s hard failure when a differing
`flake.nix` or `.envrc` already exists (it refuses to overwrite, then throws).

### Propagating a `nix/shells/default.nix` change

Two steps, both load-bearing:

1. Commit **and push** into the VM. A consuming project's input is
   `git+file:///home/xiii.guest/wrk/yolobox`, which reads the VM's git
   worktree — an unpushed or uncommitted edit is invisible to it. Editing on
   the Mac and then re-locking in the VM yields an identical lock and looks
   like a broken workflow.
2. `nix flake update yolobox` in the project. Current syntax;
   `nix flake lock --update-input` is deprecated. direnv re-enters on its own,
   since `use_flake` watches `flake.nix` and `flake.lock`.

`nixos-rebuild` plays no part: `nix/shells` is reached only from `flake.nix`'s
`devShells`/`lib.shell` outputs, and no NixOS module imports it, so dev shells
are never in the system closure.

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
