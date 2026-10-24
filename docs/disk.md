# Disk: recognising a full one, and what filled it

`/dev/vda2` is the only real filesystem. How `/boot` fills up, and how the root disk grows, are in [nix/base.md](../nix/base.md). What `yo gc` deletes, tier by tier, is in [nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md#gc-tiers). This file holds how to tell a full disk from other failures, and the incidents behind each cache `yo gc` knows about.

## Recognising a full disk

`/tmp` is a directory on `/dev/vda2`, not a tmpfs. So a full disk first shows up as the agent harness dying with `ENOSPC ... mkdir '/tmp/claude-501/...'` before any command runs.

Two facts make it survivable. First, ext4 reserves 1,057,641 blocks x 4 KiB = 4.3 GB for root that `df`'s "Avail" column does not count. The operator's `sudo`, over `yo ssh`, therefore still works while every unprivileged command dies. The agent has no `sudo` at all, so an agent session dying is not evidence either way about how full the disk actually is. Second, `yo ssh` reaches in from the Mac, which the full disk cannot hurt. That is why the remedy, `yo gc`, lives on the Mac, and why its machine tier runs first, as the operator.

## Incidents

### The first: one project's `target/`

The `rune` project's `target/` under the guest home held 41 GiB, 43% of the disk. `rune/Cargo.toml` had no `[profile.dev]`, so every dev build and every integration test linked its own unstripped binary of about 230 MiB, and cargo never prunes old hashes. Fixing `Cargo.toml` per repo would leave the next Rust project exposed, so `nix/base.nix` exports `CARGO_PROFILE_DEV_DEBUG=line-tables-only` box-wide. Its comment there carries the mechanism. Backtraces still carry file and line; stepping through variables in a debugger does not.

Runners-up: 32 GiB of dead nix store paths, 4.7 GiB podman, 2.9 GiB `/root/.cache/nix`, 2.5 GiB `~/.npm`.

### 2026-10-15: caches `yo gc` did not know about

The 400 GiB disk sat at 95-99% and `target/` was not the story. The biggest consumers were `~/.cache/go-build` at 22 GiB and `~/.cache/uv` at 10 GiB. Next came `~/.cache/nix` (the agent's own, not root's) at 3.2 GiB and the Go module caches: `~/go/pkg/mod` at 518 MiB, `~/Developer/go/pkg/mod` at 3.4 GiB, and the build cache `~/Developer/go/cache` at 106 MiB. `~/.cargo/registry` held 1.2 GiB, and a tail of gopls, goimports, golangci-lint, staticcheck, pip, pip-tools, pnpm, zig, cargo-zigbuild and opengrep caches made up the rest.

npm deduplication was measured and rejected. All `node_modules` together hold 3.8 GiB, of which 0.9 GiB is duplicate, so a dedup tool buys less than one of the Go caches' rounding error.

`/tmp/claude-1000`, Claude's scratchpads, held 49 GiB. The `q /tmp 10d` aging rule is not broken. It removes a file only when its atime, mtime and ctime are all older than ten days, and every scratchpad had been touched recently. The data is simply fresh, so `yo gc` reports it and never deletes it.

The Go caches were split because GOPATH was set only by the agent's interactive dotfiles (`~/wrk/go`, physically `~/Developer/go`). Every other process, t3 or a non-interactive ssh, fell back to Go's default `~/go`, and the module cache existed twice. `nix/lib/agent-env.nix` now sets `GOPATH=~/Developer/go` for every agent process. That is the physical spelling, so it never depends on the `~/wrk` link existing first. `nix/base.nix` creates that link with a plain `L` tmpfiles rule, because `L+` would delete a real `~/wrk` directory.

The podman share had a second cause. `virtualisation.podman.autoPrune` renders a system `podman-prune.timer` that runs as root, and this box is rootless-only, so root owned nothing and the weekly prune did nothing, silently, for as long as it existed. `nix/podman.nix` declares a user timer instead. Check it with `systemctl --user list-timers` as the agent, which must show it, and `systemctl list-timers` as the operator, which must not. The gc report used to measure `~/.local/share/containers/storage` with `du` and got 19G, but rootless podman stores files under subuids the agent cannot read. `podman system df` reported 25G (18.7G of images, 6.3G of volumes), and `du` printed 259 `du: cannot read directory` lines per measure. The report now prints `podman system df`, so images and volumes show separately and `--deep` visibly cannot reclaim the volumes.

### 2026-10-19: operator-owned caches

259G were used of 394G, and two more caches were owned by the operator, so the agent tier could not see them.

`/var/lib/systemd/coredump` held 2.1G. Among it were 259 `core.marksman.*.zst` of about 3.6M each, written between 2026-10-05 and 10-18, plus chromium and doveadm cores. marksman is the .NET Markdown LSP. pi-lens 3.8.74 (a pi extension) downloads it to `~/.pi-lens/bin/marksman` and spawns `marksman server` whenever pi edits a `.md` file. Through nix-ld it aborted with SIGABRT, "Couldn't find a valid ICU package installed on the system", because .NET loads ICU at startup and nix-ld's library path had no `icu`. pi-lens retried on a timer, one core per attempt. `nix/base.nix` now adds `icu` to `programs.nix-ld.libraries`, which fixes any downloaded .NET binary that aborts the same way, and caps coredumps with `Coredump.MaxUse = "1G"`. Its comments there carry the rest (systemd's default cap is 10% of the filesystem, about 40G here).

Recognise the shape without the journal. `coredumpctl list` showed nothing, because the journal only held the current boot. Each core carries its origin in `user.coredump.*` xattrs (`getfattr -d <file>`), and `~/.pi-lens/sessionstart.log` shows the retries.

The operator's own `~/.cache/nix` held 3.3G, of which `gitv3` was 3.2G. Those are the flake-input git fetches that every `nixos-rebuild --flake github:...` leaves behind. `gc-machine` only knew root's cache, so `yo gc --yes` now empties both. Neither is tied to a generation, which is why both run outside the booted-equals-profile guard.

### 2026-10-20: build output that no name-based rule reached

The project tier of `yo gc --deep` used to delete every `target`, `node_modules` and `.next` that `.gitignore` ignored. The objection was that a directory called `target` can hold a project's code or data, and a name is no evidence that something is build output. The live box showed the other half of the problem:

- `~/Developer/pyapp` (devbox.json and a Makefile) held `out` at 89G and `.venv` at 1.6G, which no name-based tier touches.
- `~/Developer/rune-wt-search/target` (52G) and `~/Developer/rune-wt-undo/target` (60G) were cargo's, because each carries cargo's `CACHEDIR.TAG`, and each sits beside `Cargo.toml`, `devbox.json` and a Makefile.
- `~/Developer/gotool` (go.mod, a Makefile, an `.envrc`) held `dist` at 13G.

The old tier would have reclaimed about 112G of the roughly 250G of build output and missed the rest. The tier now asks each project instead; see [nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md#gc-tiers).

The first live report of the new tier (2026-10-20) is the picture to compare against:

- `FAIL` lines for `website` and `ledger` (each has a Makefile and no make in its devbox) and for `chat/packages/crypto/target` (no cargo in its environment).
- `kept` lines for three `target` directories in `metasploit-framework` and for `rustapp/go/target`, none of them cargo's.
- `make clean would run: rm -rf bin` for gotool and a dozen Go worktrees.
- Four cargo targets: `rune-wt-crlf` at 32G, `rune-wt-undo` at 60G, `rune-wt-search` at 53G and `rustapp` at 7.1G.

It took 1m45s because every project's devbox is entered. It exits 1 for as long as any project lacks a tool it needs, so a nonzero exit from `yo gc --deep` is expected until those `devbox.json` files gain make and cargo.
