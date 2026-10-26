# yolobox-guest: yo's shell, inside the box

`nix/guest/yolobox-guest.sh` is the guest-side half of `yo`. The comments in the script carry the short reason for each function. This file keeps the incidents and the ways to recognise them. For how a full disk looks and what filled it, see [docs/disk.md](../../docs/disk.md).

## One helper, one PATH

`nix/guest.nix` builds the script into the `yolobox-guest` package, and it sits on both accounts' `PATH`. It holds the project walk, the gitconfig-root walk, the landing-dir ancestor walk, `ensure-repo`, `generations`, both `gc` tiers and the AWS probe. Before this it was about 200 lines of bash trapped inside Python string constants (`PROJECT_FIND`, `GC_MACHINE` and the rest), and nothing checked any of it. There was no shellcheck, no `bash -n`, and no test that ever exercised a single find expression.

`writeShellApplication` buys three things a Python string literal never could. A build-time shellcheck pass turns a bad find expression into a build error instead of a runtime one. It sets `set -euo pipefail`. And it pins a `runtimeInputs` PATH.

`yo` reaches every subcommand the same way, `ssh ... yolobox-guest <sub> [args]`, with one account per subcommand:

- `projects`, `landing-dir`, `ensure-repo`, `gc-user` and `aws-check` run as the agent.
- `generations` and `gc-machine` run as the operator, because ext4's root reserve, which makes any of this recoverable on a genuinely full disk, is the operator's alone.
- `net-check` runs as the operator too, the one account every box has, however far its agent setup got.

`aws-creds` is covered in [docs/aws.md](../../docs/aws.md#aws-creds).

## net-check and the gateway-blocked marker

`net-check` takes no arguments and answers two questions in two lines, `egress=` (an HTTPS fetch of `https://github.com` bounded by `--max-time 5`, `probe_egress` in the script) and `gateway=` (a DNS query for the root name servers sent to lima's gateway at 192.168.5.2 with `dig +time=2 +tries=1`, `probe_gateway_dns` in the script). Together they tell a VPN on the host that owns its default route from one that blackholes the gateway; see [docs/host-network.md](../../docs/host-network.md#vpn-shapes).

The gateway is `ok` when `dig`'s output contains `->>HEADER<<-`, which `dig` prints only when a DNS reply arrived, whatever its status (SERVFAIL counts). `dig`'s exit code is not read, because bind9 reports 0 on an ICMP unreachable or a reset connection (isc-projects/bind9 #3235 and #3615). Before the probe asked DNS, it connected to a TCP port on the host's loopback through the gateway; the egress table now rejects that, and DNS is the one gateway path it leaves open. A caller that passes an argument gets the usage line and exit 2, which is also what an old box answers a new `yo` that sends none; `yo` reports `network: not checked` for both.

A box whose own configuration blocks the gateway by design says so by creating the file `/etc/yolobox/gateway-blocked`. Its content is not read, only its existence. When the file exists, `net-check` prints `gateway=skipped` and does not probe, because a probe that is refused on purpose would be reported as a VPN fault that is not there. `egress=` still runs. `yo` turns `skipped` into a `network:` line that says the probe was skipped because the box blocks it by design, and that line carries the verdict on egress alone. The repo creates no such file itself and names no reason for it; the marker is a hook for a box's `/etc/yolobox/local.nix` or a module it imports.

`YOLOBOX_GATEWAY_BLOCKED` overrides the marker's path for the tests.

## The project walk

A project is any git repo at any depth under the guest home, not only `<group>/<repo>`. A monorepo puts one at depth 7. So the walk, `yolobox-guest projects`, reached from `pick_project` in `yo`, is bounded by pruning `BUILD_DIRS` and every hidden directory (`.git` itself included). There is no depth cap, because a depth cap has to be re-guessed every time a monorepo nests one level deeper.

That pruning is real today, but it was not always, and the gap between the two is worth recognising. `653e5e6` ("fix: remove maxdepth from fuzzy project search") deleted the `-maxdepth 5` the walk used to carry. That was a regression, because nothing replaced the depth bound it was also accidentally providing. But the defect it exposed was already there before that commit, just masked.

`-mindepth` is a global find option. It suppresses evaluation of the whole expression for every entry above the given depth, and `-prune` is a test, so it is suppressed too. The walk ran at `-mindepth 2`, so every top-level dotdir, `~/.local` included, was never tested against `-name '.*'` and never pruned. Find walked straight into whatever it held, which in the incident that surfaced this was podman's 0700 volume storage. GNU find exits 1 on any unreadable directory, and the walk's exit code used to be checked unconditionally. So the whole command aborted and the project list already gathered was thrown away with it.

Recognise that shape: `yo enter <fuzzy>` died with a find `Permission denied` and no project list at all, even though the walk had already found real projects before it reached the unreadable one. `-maxdepth 5` had hidden the `-mindepth` hole for as long as the podman volume sat below depth 5, which is what made `653e5e6` look like a clean fix at the time.

The walk is now `-mindepth 1` and prunes every top-level dotdir explicitly, rather than leaning on a maxdepth to keep it out of reach. An unreadable directory now warns, through `yo` printing find's own stderr, and the walk continues with the partial list. Only an empty list still refuses, with "no projects in the VM".

## BUILD_DIRS and LOCAL_CACHES

`BUILD_DIRS` (`node_modules`, `target`, `.next`) is the single place build-output directory names are spelled, inside the script. The project walk prunes them, because a repo living inside `node_modules` is not a project a person would pick. The Go-cache walk shares the same list. Before the move there were two lists, `PROJECT_FIND` and `GC_USER`'s `project_targets`, and neither knew the other existed.

`BUILD_DIRS` only prunes. Since 2026-10-20 it no longer decides what `gc --deep` deletes. A name match is no evidence that a directory is build output, so the project that owns the directory decides, through the clean command it defines. The names the tier deletes live in arrays of their own: one for directories removed outright, one for names removed only beside a manifest.

Pruning `BUILD_DIRS` opens a failure shape that looks exactly like the old maxdepth-cap symptom. A repo literally named `target`, `node_modules` or `.next` is invisible to the picker, and fzf says "no project matches" for a repo that plainly exists. That is the same wording the maxdepth cap used to produce for an unrelated reason. Do not assume "no project matches" means the cap is back. Check first whether the repo's own directory name collides with `BUILD_DIRS`.

`LOCAL_CACHES` is the single spelling of the cache names `gc --yes` deletes under `~/.cache`. Its rule: only caches a tool rebuilds or refetches by itself belong in it. Model weights and app data never do (`~/.cache/huggingface`, `modelscope`, `ms-playwright`, and `~/.cache/rune`, which is a macOS SDK), because losing them costs a download the box cannot necessarily repeat, or breaks a tool outright. Rust and Go caches are dropped by plain `--yes` even though a module cache needs the network to come back, because they are the bulk of the space. Cargo's `registry` and `git` are fixed paths spelled once in `cache_targets`. The Go caches have no fixed path at all; see "Guards" below.

`yo gc --help` names every entry of both arrays, and every directory and clean command the `--deep` tier can act on. `TestGcHelpNamesEverythingItDeletes` fails when the help stops naming one.

## Guards

Several guards exist because the script's inputs come from `yo` or from the filesystem the agent writes.

- `landing-dir` exits 2 with `path must be absolute` on a relative argument. Its ancestor walk strips trailing components until a directory exists. On a relative path that does not exist, the strip stops changing the string at its last component (`${d%/*}` leaves a name without a slash alone), so the loop would never end. `yo` is the other half: it runs the reported directory through `guest_path()` before using it, so a helper that answers with a path outside the guest home is refused instead of landed in.
- `remove_tree`, which the cache sweep and `gc --deep` both delete through, removes a symlink as the link. Its `chmod -R u+w` (needed for Go's read-only module cache) used to follow a link and rewrite permissions on whatever it pointed at.
- The Go caches are found by signature, not path, because GOPATH used to be set only by an interactive shell rc file (see [docs/disk.md](../../docs/disk.md#incidents)). A module cache is a `pkg/mod` holding `cache/download`. A build cache is a directory whose `README` opens with the line `go` itself writes and which has a `trim.txt` beside it. A stray README that merely starts with that line would otherwise nominate its directory for deletion. `$HOME` itself is never a cache, so a README at the top of the home cannot nominate the home. The walk prunes hidden directories and `BUILD_DIRS`, and stops at depth 5.
- The `podman system prune -f` step removes stopped containers, unused networks, dangling images and build cache, and keeps volumes. The unit description and the step comment say so in those words, because "dangling containers" read as if running state was at stake. It never takes `--volumes`, because volumes hold database state.

## Version skew

The Mac side never guesses what a box supports; it reads the exit code. A subcommand `yo` was built to call after `yolobox-guest` shipped it exits **64**, with `yolobox-guest: unknown subcommand '<sub>'` on stderr. The box is older than this Mac's `yo`. A box with no `yolobox-guest` at all exits **127**, with the shell's own "command not found" on stderr. It predates the helper entirely. Both route through `guest_helper()` and `helper_skew()` in `yo` into the same message: which version this Mac's `yo` is, which version built the box, and `yo bootstrap` as the fix.

The exit code alone is never enough in either direction. A subcommand's own child process (`podman`, `npm`) can legitimately exit 127 or 64 for reasons that have nothing to do with the helper. So both branches require the stderr marker too before reinterpreting the exit. The corollary is load-bearing: no subcommand's own stderr may ever contain the literal string `unknown subcommand`.

It follows that adding a subcommand to the script does not make it callable on its own. The box has to be rebuilt first (`yo bootstrap`, or a plain `nixos-rebuild switch` for someone iterating inside the VM). Calling it against an un-rebuilt box hits the 64 path, correctly, because from the box's point of view the subcommand really is unknown.

## gc tiers

`yo gc` reports. `--yes` runs two ordered ssh sessions, one per account, not one session doing everything. What each tier deletes is in `yo gc --help`; this section holds the reasons.

**The machine tier runs first, as the operator.** It handles journald, `nix-collect-garbage`, `nix-store --optimise`, root's nix cache, systemd coredumps and the operator's own nix cache. It runs first on purpose, because ext4's root reserve is what makes the rest of the cleanup possible on a genuinely full disk. `nix-store --optimise` runs inside the same booted-equals-profile guard as `nix-collect-garbage -d`, and `nix.settings.auto-optimise-store = true` in `nix/base.nix` hard-links new store paths as they land, instead of waiting for the next gc. The two nix caches and the coredumps are not tied to a generation, so they run outside the guard.

**The user tier then runs as the agent.** It handles the npm cache, rootless podman, the cache tier, and under `--deep --yes` the project tier. The split is not cosmetic. The npm cache, podman's storage and every project checkout live under the agent's home, not the operator's. A `yo gc` that only ran as the operator would measure and clean an empty home and still report success, a silent no-op that never touches the account actually holding the state. The cache tier runs without `--deep`, because none of it is project state.

**Podman is measured with `podman system df`,** not `du`. Rootless podman stores files under subuids the agent cannot read, so `du` undercounts and prints one `du: cannot read directory` line per unreadable path. A failing `podman system df` is a `FAIL` line, not a silent gap.

**`nix-collect-garbage -d` deletes the running generation** when a switch half-failed. `yo gc` compares the two readlinks (see [nix/base.md](../base.md#the-esp-holds-one-kernel)) and reports a skip. A skip means the box needs a repaired switch before it needs a garbage collection.

**`.devbox/virtenv` is not touched.** Hidden directories other than the names the tier removes are pruned, and `virtenv` is devbox's toolchain, not build output.

### The project tier

Since 2026-10-20 the tier asks each project instead of matching names (the incident is in [docs/disk.md](../../docs/disk.md#incidents)). It starts from every git repo under the home, the same walk `projects` uses, and treats each one as a project root.

- A root with a `Makefile`, `GNUmakefile` or `makefile` whose `make -n clean` succeeds gets `make clean`.
- A root with a `go.mod` gets `go clean`. Without flags it removes only the package's own build outputs in that directory, so it reclaims little and is there for completeness.
- A `target` directory beside a `Cargo.toml` gets `cargo clean`, run in that directory.
- Only `node_modules` and `.venv` are removed outright, plus a `.next` that has a `package.json` beside it.

Two traps shape the guard. Deleting a build directory the project's `.gitignore` does not match dirties the checkout, and every later `git push yolobox` is refused with no obvious link back. `example.com` anchors `/node_modules`, so a nested `web/node_modules` is tracked. Hence every directory the tier removes, or leaves for cargo to remove, passes `git check-ignore` first, and one git does not ignore fails with `kept <path>: git does not ignore it` and exit 1.

The guard also accepts a directory that ignores itself. uv and `python -m venv` write a `.gitignore` of `*` into the `.venv`, which git does not report as ignoring the directory. So the guard asks instead whether git tracks or sees anything inside it (`git ls-files` and `git status --untracked-files=all` both empty).

`make clean` and `go clean` are not guarded, because they are the project's own definition of clean. `cargo clean` itself removes the whole `target` directory, the same as `rm -rf` would. The protection is the detection, a `Cargo.toml` beside the directory, not the tool. `cargo clean` acts on whatever cargo considers the target directory (`CARGO_TARGET_DIR`, `build.target-dir`, a workspace root), which may not be the `target` the guard checked.

The guard runs when a directory's turn comes, after the cleans before it. A Makefile whose `clean` does `rm -rf target` (common) has already removed the directory by then. `git check-ignore` on a path that no longer exists exits 1 for a directory-only pattern like `target/`, which used to read as `kept ...: git does not ignore it` for a directory that was already gone. Now a directory missing at apply time prints `ok:   already gone: <path>` and gets neither the guard nor the action.

`.venv` is deleted, which costs basedpyright its environment (see `nix/lsp.nix`) until the project's own `uv sync` or devbox recreates it, and that needs network.

### The project's own environment

`cargo`, `go` and `make` are on no box-wide PATH, because the box has no toolchain of its own. Each project's devbox supplies one, so each command runs through `in_project_env`: `devbox run -q -- <cmd>` when the root has a `devbox.json`, else `direnv exec <root> <cmd>` when it has an `.envrc`, else the command directly. `devbox run` takes about half a second per project when its packages are installed, and needs network when they are not. Stdin is `/dev/null` for all three forms. The plan is built inside `while read` loops over a NUL-separated file, and a tool that reads stdin would otherwise eat the roots after its own.

A tool that does not resolve in that environment is a `FAIL` line naming the tool and the root. Only that tool's action in that root is skipped: a missing `cargo` keeps the `target`, while the same root's `make clean`, `go clean` and `node_modules` removal still run. The remedy is adding the tool to the project's `devbox.json` or `.envrc`, or cleaning by hand.

### Report mode versus apply

Report mode, `yo gc --deep` without `--yes`, measures each cargo target, `node_modules`, `.venv` and `.next` with the action named after it. It prints `<root>: make clean would run:` followed by the output of `make -n clean`, and `<root>: go clean`, and deletes nothing. Apply runs `podman system prune -af`, then every `make clean`, every `cargo clean` and every `go clean`, and only then the outright removals, each through `step`.

Report mode runs no clean, but it does enter each project's environment for the tool probes and `make -n clean`. So a report is not free of side effects. `devbox run` can install the project's packages (network) and rewrite `devbox.lock`, and GNU make's `-n` still evaluates `$(shell ...)` and remakes included makefiles.

### Output shapes that look like faults and are not

- `kept <path>: no Cargo.toml beside it, so it is not cargo's build output` for a directory that happens to be called `target`. It is a data directory, and the tier has declined to guess. The same holds for a `.next` with no `package.json`.
- `kept <path>: not inside a git repository` for a directory that sits in no repo at all, which no project owns and no `.gitignore` vouches for.
- A `FAIL` line naming a tool and a root, saying the tool is not in that root's environment. The project's `devbox.json` or `.envrc` lacks the tool, and only that tool's action in that project was skipped.
- `make clean` output appearing inside `yo gc`. That is the Makefile's own recipe running, so whatever the project's clean prints is what the operator sees.
