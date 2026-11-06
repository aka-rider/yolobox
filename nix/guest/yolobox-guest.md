# yolobox-guest: yo's shell, inside the box

`nix/guest/yolobox-guest.sh` is the guest-side half of `yo`. The comments in the script carry the short reason for each function. This file keeps the incidents and the ways to recognise them. For how a full disk looks and what filled it, see [docs/disk.md](../../docs/disk.md).

## One helper, one PATH

`nix/guest.nix` builds the script into the `yolobox-guest` package, and it sits on both accounts' `PATH`. It holds the project walk, the gitconfig-root walk, the landing-dir ancestor walk, both `gc` tiers and the AWS probe.

`writeShellApplication` buys three things a Python string literal never could. A build-time shellcheck pass turns a bad find expression into a build error instead of a runtime one. It sets `set -euo pipefail`. And it pins a `runtimeInputs` PATH.

`yo` reaches every subcommand the same way, `ssh ... yolobox-guest <sub> [args]`, with one account per subcommand:

- `projects`, `landing-dir`, `gc-user` and `aws-check` run as the agent.
- `gc-machine` runs as the operator, because ext4's root reserve, which makes any of this recoverable on a genuinely full disk, is the operator's alone.
- `net-check` runs as the operator too, the one account every box has, however far its agent setup got.

`aws-creds` is covered in [docs/aws.md](../../docs/aws.md#aws-creds). `rebuild` is the one subcommand `yo` never calls; a person runs it inside the box, as the operator (below).

## rebuild

`yolobox-guest rebuild switch` (or `boot`) is the one way to rebuild a box from inside it. Before it, a hand rebuild meant typing `sudo YOLOBOX_USERNAME=... YOLOBOX_HOSTNAME=... YOLOBOX_FLAKE_REF=... nixos-rebuild switch --impure --flake '<ref>#yolobox'` correctly. A wrong or missing hostname renamed an `-m` machine back to `yolobox`, and a different ref quietly moved the box to another tree. The helper takes those values from what the last build stamped: `/etc/yolobox/flake-ref` and `/etc/yolobox/hostname`, both written by `nix/base.nix` on every build. The username is the caller's own (`id -un`). Then it runs `sudo env YOLOBOX_USERNAME=... YOLOBOX_HOSTNAME=... YOLOBOX_FLAKE_REF=... flock -n -E 75 /run/yolobox-rebuild.lock nixos-rebuild <action> --impure --refresh --flake <ref>#yolobox`, and `nixos-rebuild`'s exit code is the helper's. The lock is why an interrupted `yo bootstrap` cannot be doubled: its ssh has no pty, so when the connection drops the `nixos-rebuild` it started keeps running in the box, and a rerun would start a second one beside it. `yo bootstrap` takes the same lock (`REBUILD_LOCK` and `REBUILD_BUSY_EXIT` in `yolobox_cli/release.py`, equal to the script's by a consistency test), `flock -n` refuses a second run at once instead of waiting, and exit 75 makes both print that another rebuild is running. The lock file is in `/run`, which only root writes, so `flock` runs under `sudo` and the variables are set before it and reach `nixos-rebuild` through `exec`. `YOLOBOX_REBUILD_LOCK` overrides the path for the tests. The rebuild hint `yo` prints for a person to paste does not take the lock. `--refresh` is there because a stamped ref may name a branch, and nix would otherwise reuse an hour-old fetch of it and build an old commit; the flags are `yo`'s `NIX_REBUILD_FLAGS`, kept equal by a consistency test (see [docs/distribution.md](../../docs/distribution.md#a-branch-that-moved-built-at-its-old-commit)). nixos-rebuild prints no commit; `nix flake metadata --refresh <ref>` in the box names the one a rebuild now builds.

It refuses, without running `sudo`:

- anything but exactly one argument, `switch` or `boot`, with the usage line and exit 2. `test`, `dry-activate` and `--rollback` are refused too. Only `switch` and `boot` make the next boot run what was built; `test` activates a system no boot entry names, and the helper's job is the rebuild a person needs after editing `/etc/yolobox/local.nix`, not every `nixos-rebuild` action.
- a caller whose uid is not 501, the operator's (exit 2, "run as the operator"). Root gets no say here, and the agent has no `sudo` anyway.
- a stamp that is missing, empty or holds more than one line, naming the file (exit 1). A trailing newline is fine.
- a flake ref that is not exactly `github:OWNER/REPO/REF` (`FLAKE_REF_RE`, equal to `yo`'s `GITHUB_FLAKE_RE`), naming the file (exit 1). The stamp is a root-owned file the agent cannot write, so this is defence in depth for the `--flake` argument: whatever the file holds, the rebuild never gets a `path:` ref, a second flake attribute after a `#`, or a word starting with `-`.

To move the box to another release or branch, run `yo bootstrap --version <ref>` on the host: the stamp then names the new ref, and the next `rebuild` reuses it. `yo bootstrap` and `yo`'s printed rebuild hint do not go through this helper, because they hold the host's facts already and the helper may be missing from an older box. `TestGuestRebuildMatchesYoAndTheBox` keeps the helper's variable names, ref pattern and operator uid equal to `yo`'s and `nix/base.nix`'s. `YOLOBOX_STAMPS` overrides `/etc/yolobox` for the tests.

## net-check and the gateway-blocked marker

`net-check` takes no arguments and answers three questions in three lines, in this order: `egress=` (an HTTPS fetch of `https://github.com` bounded by `--max-time 5`, `probe_egress` in the script), `gateway=` (a DNS query for the root name servers sent to lima's gateway at 192.168.5.2 with `dig +time=2 +tries=1`, `probe_gateway_dns` in the script) and `rootdisk=` (below). The first two together tell a VPN on the host that owns its default route from one that blackholes the gateway; see [docs/host-network.md](../../docs/host-network.md#vpn-shapes).

The gateway is `ok` when `dig`'s output contains `->>HEADER<<-`, which `dig` prints only when a DNS reply arrived, whatever its status (SERVFAIL counts). `dig`'s exit code is not read, because bind9 reports 0 on an ICMP unreachable or a reset connection (isc-projects/bind9 #3235 and #3615). Before the probe asked DNS, it connected to a TCP port on the host's loopback through the gateway; the egress table now rejects that, and DNS is the one gateway path it leaves open. A caller that passes an argument gets the usage line and exit 2, which is also what an old box answers a new `yo` that sends none; `yo` reports `network: not checked` for both.

A box whose own configuration blocks the gateway by design says so by creating the file `/etc/yolobox/gateway-blocked`. Its content is not read, only its existence. When the file exists, `net-check` prints `gateway=skipped` and does not probe, because a probe that is refused on purpose would be reported as a VPN fault that is not there. `egress=` still runs. `yo` turns `skipped` into a `network:` line that says the probe was skipped because the box blocks it by design, and that line carries the verdict on egress alone. The repo creates no such file itself and names no reason for it; the marker is a hook for a box's `/etc/yolobox/local.nix` or a module it imports.

`YOLOBOX_GATEWAY_BLOCKED` overrides the marker's path for the tests.

`rootdisk=full` means `/` has under 64 MiB (`ROOT_MIN_FREE_MIB`) left for root, read by `root_free_mib` from `stat -f -c '%f %S' /`. That counts the blocks ext4 reserves for root, which `df`'s Avail column leaves out, because the processes a full disk breaks (dhcpcd, tailscaled) run as root and fail only when those blocks are gone too. A full disk first shows as a dead network, so `yo status` prints it instead of the network verdict (see [docs/disk.md](../../docs/disk.md#a-full-disk-looks-like-a-dns-failure)). A `stat` that fails or prints something that is not two numbers fails `net-check` with the message, and `yo` reports `network: not checked`; it is never read as "not full". A box built before this line existed prints none, and `yo` reads that as unknown, not full.

## The project walk

A project is any git repo at any depth under the guest home, not only `<group>/<repo>`. A monorepo can nest one many levels deep. So the walk, `yolobox-guest projects`, reached from `pick_project` in `yo`, is bounded by pruning `BUILD_DIRS` and every hidden directory (`.git` itself included). There is no depth cap, because a depth cap has to be re-guessed every time a monorepo nests one level deeper.

`-mindepth` is a global find option. It suppresses evaluation of the whole expression for every entry above the given depth, and `-prune` is a test, so it is suppressed too. A walk at `-mindepth 2` never tests a top-level dotdir, `~/.local` included, against `-name '.*'`, so it never prunes it and find walks straight into whatever it holds, such as podman's 0700 volume storage. GNU find exits 1 on any unreadable directory, so a walk that checks its exit code unconditionally aborts and throws away the project list already gathered.

Recognise that shape: `yo enter <fuzzy>` dies with a find `Permission denied` and no project list at all, even though the walk found real projects before it reached the unreadable one. A depth cap hides the hole for as long as the unreadable directory sits below it, which is why a cap is not a fix.

The walk is `-mindepth 1` and prunes every top-level dotdir explicitly, rather than leaning on a maxdepth to keep it out of reach. An unreadable directory now warns, through `yo` printing find's own stderr, and the walk continues with the partial list. Only an empty list still refuses, with "no projects in the VM".

## BUILD_DIRS and LOCAL_CACHES

`BUILD_DIRS` (`node_modules`, `target`, `.next`) is the single place build-output directory names are spelled, inside the script. The project walk prunes them, because a repo living inside `node_modules` is not a project a person would pick. The Go-cache walk shares the same list. One list serves the project walk and the cache walk, so they cannot drift apart.

`BUILD_DIRS` only prunes. It does not decide what `gc --deep` deletes. A name match is no evidence that a directory is build output, so the project that owns the directory decides, through the clean command it defines. The names the tier deletes live in arrays of their own: one for directories removed outright, one for names removed only beside a manifest.

Pruning `BUILD_DIRS` opens a failure shape that looks exactly like a project hidden by the picker's maxdepth cap. A repo literally named `target`, `node_modules` or `.next` is invisible to the picker, and fzf says "no project matches" for a repo that plainly exists. That is the same wording the maxdepth cap produces for an unrelated reason. Do not assume "no project matches" means the cap is back. Check first whether the repo's own directory name collides with `BUILD_DIRS`.

`LOCAL_CACHES` is the single spelling of the cache names `gc --yes` deletes under `~/.cache`. Its rule: only caches a tool rebuilds or refetches by itself belong in it. Model weights and app data never do (`~/.cache/huggingface`, `modelscope` and `ms-playwright`), because losing them costs a download the box cannot necessarily repeat, or breaks a tool outright. Rust and Go caches are dropped by plain `--yes` even though a module cache needs the network to come back, because they are the bulk of the space. Cargo's `registry` and `git` are fixed paths spelled once in `cache_targets`. The Go caches have no fixed path at all; see "Guards" below.

The host-side `yo cp` download drops the union of `BUILD_DIRS` and `REMOVED_OUTRIGHT`, under the `NEEDS_MANIFEST` rule, from a pulled tree before it lands (see `yo.md`, "Downloads never land build output"). The spellings are kept equal by the consistency tests, so a new name goes into the shell arrays and `yo`'s `BUILD_OUTPUT` together.

`yo gc --help` names every entry of both arrays, and every directory and clean command the `--deep` tier can act on. `TestGcHelpNamesEverythingItDeletes` fails when the help stops naming one.

## Guards

Several guards exist because the script's inputs come from `yo` or from the filesystem the agent writes.

- `landing-dir` exits 2 with `path must be absolute` on a relative argument. Its ancestor walk strips trailing components until a directory exists. On a relative path that does not exist, the strip stops changing the string at its last component (`${d%/*}` leaves a name without a slash alone), so the loop would never end. `yo` is the other half: it runs the reported directory through `guest_path()` before using it, so a helper that answers with a path outside the guest home is refused instead of landed in.
- `remove_tree`, which the cache sweep and `gc --deep` both delete through, removes a symlink as the link. Its `chmod -R u+w` (needed for Go's read-only module cache) must never follow a link and rewrite permissions on whatever it points at.
- The Go caches are found by signature, not path, because a project or shell rc file can point `GOPATH` anywhere; the default is `~/go` (see [docs/disk.md](../../docs/disk.md#incidents)). A module cache is a `pkg/mod` holding `cache/download`. A build cache is a directory whose `README` opens with the line `go` itself writes and which has a `trim.txt` beside it. A stray README that merely starts with that line would otherwise nominate its directory for deletion. `$HOME` itself is never a cache, so a README at the top of the home cannot nominate the home. The walk prunes hidden directories and `BUILD_DIRS`, and stops at depth 5.
- The `podman system prune -f` step removes stopped containers, unused networks, dangling images and build cache, and keeps volumes. The unit description and the step comment say so in those words, because "dangling containers" read as if running state was at stake. It never takes `--volumes`, because volumes hold database state.

## Version skew

The Mac side never guesses what a box supports; it reads the exit code. A subcommand `yo` was built to call after `yolobox-guest` shipped it exits **64**, with `yolobox-guest: unknown subcommand '<sub>'` on stderr. The box is older than this Mac's `yo`. A box with no `yolobox-guest` at all exits **127**, with the shell's own "command not found" on stderr. It predates the helper entirely. Both route through `guest_helper()` and `helper_skew()` in `yo` into the same message: which version this Mac's `yo` is, which version built the box, and `yo bootstrap` as the fix.

The exit code alone is never enough in either direction. A subcommand's own child process (`podman`, `npm`) can legitimately exit 127 or 64 for reasons that have nothing to do with the helper. So both branches require the stderr marker too before reinterpreting the exit. The corollary is load-bearing: no subcommand's own stderr may ever contain the literal string `unknown subcommand`.

It follows that adding a subcommand to the script does not make it callable on its own. The box has to be rebuilt first (`yo bootstrap`, or `yolobox-guest rebuild switch` for someone iterating inside the VM). Calling it against an un-rebuilt box hits the 64 path, correctly, because from the box's point of view the subcommand really is unknown.

## gc tiers

`yo gc` reports. `--yes` runs two ordered ssh sessions, one per account, not one session doing everything. What each tier deletes is in `yo gc --help`; this section holds the reasons.

**The machine tier runs first, as the operator.** It handles journald, `nix-collect-garbage`, `nix-store --optimise`, root's nix cache, systemd coredumps and the operator's own nix cache. It runs first on purpose, because ext4's root reserve is what makes the rest of the cleanup possible on a genuinely full disk. `nix-store --optimise` runs inside the same booted-equals-profile guard as `nix-collect-garbage -d`, and `nix.settings.auto-optimise-store = true` in `nix/base.nix` hard-links new store paths as they land, instead of waiting for the next gc. The two nix caches and the coredumps are not tied to a generation, so they run outside the guard.

**The user tier then runs as the agent.** It handles the npm cache, rootless podman, the cache tier, and under `--deep --yes` the project tier. The split is not cosmetic. The npm cache, podman's storage and every project checkout live under the agent's home, not the operator's. A `yo gc` that only ran as the operator would measure and clean an empty home and still report success, a silent no-op that never touches the account actually holding the state. The cache tier runs without `--deep`, because none of it is project state.

**Podman is measured with `podman system df`,** not `du`. Rootless podman stores files under subuids the agent cannot read, so `du` undercounts and prints one `du: cannot read directory` line per unreadable path. A failing `podman system df` is a `FAIL` line, not a silent gap.

**`nix-collect-garbage -d` deletes the running generation** when a switch half-failed. `yo gc` compares the two readlinks (see [nix/base.md](../base.md#the-esp-holds-one-kernel)) and reports a skip. A skip means the box needs a repaired switch before it needs a garbage collection.

**`.devbox/virtenv` is not touched.** Hidden directories other than the names the tier removes are pruned, and `virtenv` is devbox's toolchain, not build output.

### The project tier

The tier asks each project instead of matching names (the incident is in [docs/disk.md](../../docs/disk.md#incidents)). It starts from every git repo under the home, the same walk `projects` uses, and treats each one as a project root.

- A root with a `Makefile`, `GNUmakefile` or `makefile` whose `make -n clean` succeeds gets `make clean`.
- A root with a `go.mod` gets `go clean`. Without flags it removes only the package's own build outputs in that directory, so it reclaims little and is there for completeness.
- A `target` directory beside a `Cargo.toml` gets `cargo clean`, run in that directory.
- Only `node_modules` and `.venv` are removed outright, plus a `.next` that has a `package.json` beside it.

Two traps shape the guard. Deleting a build directory the project's `.gitignore` does not match dirties the checkout, and every later push into it is refused with no obvious link back (see [nix/base.md](../base.md#pushing-into-a-vm-checkout)). A `.gitignore` that anchors `/node_modules` ignores only the top-level one, so a nested `web/node_modules` is tracked. Hence every directory the tier removes, or leaves for cargo to remove, passes `git check-ignore` first, and one git does not ignore fails with `kept <path>: git does not ignore it` and exit 1.

The guard also accepts a directory that ignores itself. uv and `python -m venv` write a `.gitignore` of `*` into the `.venv`, which git does not report as ignoring the directory. So the guard asks instead whether git tracks or sees anything inside it (`git ls-files` and `git status --untracked-files=all` both empty).

`make clean` and `go clean` are not guarded, because they are the project's own definition of clean. `cargo clean` itself removes the whole `target` directory, the same as `rm -rf` would. The protection is the detection, a `Cargo.toml` beside the directory, not the tool. `cargo clean` acts on whatever cargo considers the target directory (`CARGO_TARGET_DIR`, `build.target-dir`, a workspace root), which may not be the `target` the guard checked.

The guard runs when a directory's turn comes, after the cleans before it. A Makefile whose `clean` does `rm -rf target` (common) has already removed the directory by then. `git check-ignore` on a path that no longer exists exits 1 for a directory-only pattern like `target/`, which would read as `kept ...: git does not ignore it` for a directory that was already gone. So a directory missing at apply time prints `ok:   already gone: <path>` and gets neither the guard nor the action.

`.venv` is deleted, which costs basedpyright its environment (see `nix/lsp.nix`) until the project's own `uv sync` or devbox recreates it, and that needs network.

### The project's own environment

`cargo`, `go` and `make` are on no box-wide PATH, because the box has no toolchain of its own. Each project's devbox supplies one, so each command runs through `in_project_env`: `devbox run -q -- <cmd>` when the root has a `devbox.json`, else the command directly. A project's `devbox.json` is the only project environment the box knows; there is no `.envrc` or direnv. `devbox run` is quick when a project's packages are installed, and needs network when they are not. Stdin is `/dev/null` for both forms. The plan is built inside `while read` loops over a NUL-separated file, and a tool that reads stdin would otherwise eat the roots after its own.

A tool that does not resolve in that environment is a `FAIL` line naming the tool and the root. Only that tool's action in that root is skipped: a missing `cargo` keeps the `target`, while the same root's `make clean`, `go clean` and `node_modules` removal still run. The remedy is adding the tool to the project's `devbox.json`, or cleaning by hand.

### Report mode versus apply

Report mode, `yo gc --deep` without `--yes`, measures each cargo target, `node_modules`, `.venv` and `.next` with the action named after it. It prints `<root>: make clean would run:` followed by the output of `make -n clean`, and `<root>: go clean`, and deletes nothing. Apply runs `podman system prune -af`, then every `make clean`, every `cargo clean` and every `go clean`, and only then the outright removals, each through `step`.

Report mode runs no clean, but it does enter each project's environment for the tool probes and `make -n clean`. So a report is not free of side effects. `devbox run` can install the project's packages (network) and rewrite `devbox.lock`, and GNU make's `-n` still evaluates `$(shell ...)` and remakes included makefiles.

### Output shapes that look like faults and are not

- `kept <path>: no Cargo.toml beside it, so it is not cargo's build output` for a directory that happens to be called `target`. It is a data directory, and the tier has declined to guess. The same holds for a `.next` with no `package.json`.
- `kept <path>: not inside a git repository` for a directory that sits in no repo at all, which no project owns and no `.gitignore` vouches for.
- A `FAIL` line naming a tool and a root, saying the tool is not in that root's environment. The project's `devbox.json` lacks the tool, and only that tool's action in that project was skipped.
- `make clean` output appearing inside `yo gc`. That is the Makefile's own recipe running, so whatever the project's clean prints is what the operator sees.
