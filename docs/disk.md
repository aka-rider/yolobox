# Disk: recognising a full one, and what filled it

`/dev/vda2` is the only real filesystem. How `/boot` fills up, and how the root disk grows, are in [nix/base.md](../nix/base.md). What `yo gc` deletes, tier by tier, is in [nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md#gc-tiers). This file holds how to tell a full disk from other failures, and the places `yo gc` knows to look, with how to recognise each.

## Recognising a full disk

`/tmp` is a directory on `/dev/vda2`, not a tmpfs. So a full disk first shows up as the agent harness dying with `ENOSPC ... mkdir '/tmp/claude-501/...'` before any command runs.

Two facts make it survivable. First, ext4 reserves a share of the blocks for root (5% unless tuned), and `df`'s "Avail" column does not count them. The operator's `sudo`, over `yo ssh`, therefore still works while every unprivileged command dies. The agent has no `sudo` at all, so an agent session dying is not evidence either way about how full the disk actually is. Second, `yo ssh` reaches in from the host, which the full disk cannot hurt. That is why the remedy, `yo gc`, lives on the host, and why its machine tier runs first, as the operator.

## Incidents

Each entry below is a place that fills the disk, how to recognise it, and what the box does about it.

### A project's `target/`

Symptom: a Rust project's `target/` under the guest home holds a large share of the disk, because every dev build and every integration test links its own unstripped binary and cargo never prunes old hashes. Fixing `Cargo.toml` per repo would leave the next Rust project exposed, so `nix/base.nix` exports `CARGO_PROFILE_DEV_DEBUG=line-tables-only` box-wide. Backtraces still carry file and line; stepping through variables in a debugger does not.

Other usual suspects are dead nix store paths, podman storage, root's `~/.cache/nix` and `~/.npm`.

### Caches `yo gc` knows about

A full disk need not be a `target/` problem. Common large caches include `~/.cache/go-build`, `~/.cache/uv`, `~/.cache/nix` (the agent's own, not root's), the Go module cache (`~/go/pkg/mod`, Go's default `GOPATH`) and `~/.cargo/registry`, then a tail of gopls, goimports, golangci-lint, staticcheck, pip, pip-tools, pnpm, zig, cargo-zigbuild and opengrep caches.

`/tmp/claude-1000`, Claude's scratchpads, can hold a lot too. The `q /tmp 10d` aging rule is not broken when it seems to leave them alone. It removes a file only when its atime, mtime and ctime are all older than ten days, and an active scratchpad is touched more often than that. So `yo gc` reports the scratchpads and never deletes them.

The podman share can fail to shrink for a second reason. `virtualisation.podman.autoPrune` renders a system `podman-prune.timer` that runs as root, and the box is rootless-only, so root owns nothing and the weekly prune does nothing, silently. `nix/podman.nix` declares a user timer instead. Check it with `systemctl --user list-timers` as the agent, which must show it, and `systemctl list-timers` as the operator, which must not. `du` on `~/.local/share/containers/storage` under-counts, because rootless podman stores files under subuids the agent cannot read: `du` prints one `du: cannot read directory` line per unreadable directory and a total that leaves those out. The gc report prints `podman system df` instead, so images and volumes show separately and `--deep` visibly cannot reclaim the volumes.

### Operator-owned caches

Two more caches are owned by the operator, so the agent tier cannot see them.

`/var/lib/systemd/coredump` fills with cores. One cause to recognise is a flood of `core.marksman.*.zst`. marksman is the .NET Markdown LSP. The pi-lens extension for pi downloads it to `~/.pi-lens/bin/marksman` and spawns `marksman server` whenever pi edits a `.md` file. Through nix-ld it aborts with SIGABRT, "Couldn't find a valid ICU package installed on the system", because .NET loads ICU at startup and nix-ld's library path had no `icu`. pi-lens retries on a timer, one core per attempt. `nix/base.nix` adds `icu` to `programs.nix-ld.libraries`, which fixes any downloaded .NET binary that aborts the same way, and caps coredumps with `Coredump.MaxUse = "1G"`. Its comments there carry the rest (systemd's default cap is 10% of the filesystem, which is a lot on a large disk).

Recognise the shape without the journal. `coredumpctl list` can show nothing, because the journal may only hold the current boot. Each core carries its origin in `user.coredump.*` xattrs (`getfattr -d <file>`), and `~/.pi-lens/sessionstart.log` shows the retries.

The operator's own `~/.cache/nix` fills with `gitv3`. Those are the flake-input git fetches that every `nixos-rebuild --flake github:...` leaves behind. `gc-machine` empties both root's cache and the operator's. Neither is tied to a generation, which is why both run outside the booted-equals-profile guard.

### Build output that no name-based rule reaches

A directory called `target` can hold a project's code or data, and a name is no evidence that something is build output. A tier that deleted every ignored `target`, `node_modules` and `.next` was therefore wrong in both directions. It could delete data, and it missed build output that has other names: a Python project's `out` directory and `.venv`, a Go project's `dist`, and the `target/` of each git worktree of one Rust project. The project tier of `yo gc --deep` asks each project instead; see [nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md#gc-tiers).

What a `--deep` report line means:

- `FAIL`: a project has a Makefile and no make in its devbox, or a nested `target` whose environment has no cargo. Add the tool to that project's `devbox.json`.
- `kept`: a `target` directory that is not cargo's (no `Cargo.toml` beside it), left alone on purpose.
- `make clean would run: ...`: the output of `make -n clean`, so you see what the project's own clean would delete before `--yes`.
- A cargo target with its size: reclaimable by `cargo clean`.

The report is slow, because every project's devbox is entered. `yo gc --deep` exits 1 for as long as any project lacks a tool it needs, so a nonzero exit is expected until those `devbox.json` files gain make and cargo.
