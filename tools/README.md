# tools/ — the module library

**The one-line promise: to add or remove a tool, drop or delete one `NN-name.sh`
file in this directory. Nothing else changes** — no `Dockerfile` edit, no smoke
list to update by hand, no separate probe to add. `grep` finds
a tool's whole identity in one file.

## The module contract

Each `tools/NN-name.sh` is a shell fragment that is **sourced**, not executed. It
sets some variables and optionally defines functions, grouped into three
facets — a module only needs the facets it actually uses; a standalone binary
like `yq` needs only the install facet, while an agent harness typically uses
all three.

### Install facet — fetching and registering the tool at build time

| Symbol | Kind | Meaning |
|---|---|---|
| `TOOL_NAME` | var, **required** | short id, used in log lines (`--- <phase> <TOOL_NAME> ---`) |
| `TOOL_VERSION` | var, optional | pinned version; empty = floats. **This is where a version pin lives now** — there is no `Dockerfile` `ARG` block for it anymore |
| `TOOL_APT` | array, optional | apt package names contributed by this module, merged into the ONE `apt-get install` batch (phase 2) |
| `TOOL_SOURCES` | array, optional | the upstream URL(s) this module fetches from, recorded next to `TOOL_VERSION` so a version bump or source change is visible in one place |
| `TOOL_SMOKE` | array, optional | commands run as the box user to prove the tool resolves. This is what `tools/smoke.sh` and the build's smoke gate consume |
| `tool_prepare()` | fn, optional | runs FIRST, before any repo registration. Only `02-user.sh` uses it |
| `tool_apt_repo()` | fn, optional | registers a third-party apt repo (key + sources list); runs BEFORE the apt batch |
| `tool_install()` | fn, optional | everything else — fetch a binary, `npm install -g`, `cargo install`, etc.; runs AFTER the apt batch, in filename order |

### Runtime facet — environment, `PATH`, and cache dirs the tool needs live

| Symbol | Kind | Meaning |
|---|---|---|
| `TOOL_ENV` | array, optional | `KEY=VALUE` pairs baked into the image as `ENV` (one instruction per variable — see the drift-guard note below). Uses the `@HOME@` placeholder, never `$HOME` |
| `TOOL_PATH` | array, optional | directories prepended to the image-wide `PATH`, ahead of the fixed system tail (`tools/lib.sh`'s `YB_FIXED_PATH_TAIL`) |
| `TOOL_HOME_DIRS` | array, optional | cache/state dirs to pre-create under `$HOME` on first launch of a fresh home volume — **bare-relative** (`go/pkg/mod`), never `@HOME@`-prefixed (see the placeholder rule below) |
| `TOOL_RUNTIME_ENV` | array, optional | `KEY=VALUE` pairs applied only at `docker exec`/launch time, overriding the image `ENV` for that one invocation. Uses `@HOME@`. Exists because a value can legitimately differ between build time (a system path) and run time (a path inside the home volume) — rust is the example, see `12-rust.sh` |
| `tool_seed()` | fn, optional | idempotent runtime seeding on a fresh (or existing) home volume — see the constraint below |

### Harness facet — MCP config and herd/agent-state reporting

| Symbol | Kind | Meaning |
|---|---|---|
| `TOOL_MCP_PATH` | var, optional | absolute path `mcp/render.sh` writes this harness's rendered MCP config to |
| `TOOL_MCP_JQ` | var, optional | the `jq` filter that reshapes the canonical merged fragment map into this harness's native schema. **Must be single-quoted** — see below |
| `TOOL_MCP_MODE` | var, optional | file mode for the rendered config; defaults to `0444` if `TOOL_MCP_PATH` is set and this isn't |
| `TOOL_REPORT` | var, optional | this harness's herd-reporting capability: `full` or `none`, with the reason recorded as a comment beside it |
| `tool_configure()` | fn, optional | build-time, root, runs inside `install-all.sh` before the MCP render layer — writes baked-in config that isn't part of the generated `ENV` region (e.g. Claude Code's managed hooks settings, pi's JS herd-report extension) |

### Why `tool_prepare()` exists

The box user must exist **before** `tool_apt_repo()` runs `gpg --dearmor`. `HOME`
is set to `/home/${USERNAME}` image-wide from the first `ENV` layer, but
`ubuntu:26.04` ships only `/home/ubuntu` — until `useradd -m` runs, `$HOME` points
at a directory that does not exist, and `gpg` wants to create `$HOME/.gnupg`
there. `02-user.sh` uses `tool_prepare()`, not `tool_install()`, to create the box
user early enough to satisfy every later `tool_apt_repo()`.

## The phase order (`tools/install-all.sh`, run as root at build)

1. **Phase 0 — bootstrap.** `apt-get install ca-certificates gnupg curl`,
   hardcoded, not module-driven. `ubuntu:26.04` ships **none** of the three, yet
   `yb_apt_repo()` needs all three (it curls a key over TLS and pipes it to `gpg
   --dearmor`). This is a fixed three-package bootstrap, not a violation of the
   one-apt-batch rule — tool packages still land in exactly one batch (phase 2).
2. **Phase 0b — `GNUPGHOME`.** Exports `GNUPGHOME=/tmp/gnupg` (created, mode 700)
   for the duration of the repo-registration phases, belt-and-braces alongside
   `tool_prepare()`.
3. **Phase 0c — `tool_prepare`.** Per module, in order: reset state, source, run
   `tool_prepare` if defined. This is where `02-user.sh` creates the box user —
   before phase 1.
4. **Phase 1 — `tool_apt_repo`.** Per module, in order: reset state, source, run
   `tool_apt_repo` if defined. Registers third-party apt repos.
5. **Phase 2 — the single apt batch.** One `apt-get update && apt-get install`
   over every module's `TOOL_APT`, plus locale generation. This is the whole
   point of collecting `TOOL_APT` per module instead of apt-installing inline —
   `nodejs` (from `10-node.sh`) must exist before any later module runs `npm
   install`, so ordering here is repos → one batch → per-module install.
6. **Phase 3 — `tool_install`.** Per module, in filename (`LC_ALL=C`) order:
   reset state, source, run `tool_install` if defined.
7. **Cleanup.** `rm -rf /var/lib/apt/lists/*` (after phase 3, not phase 2 —
   Playwright's `--with-deps` repopulates the apt lists and must be cleaned up
   after), `chmod -R a+rX /usr/local /opt`, `chown -R` `/usr/local` only to the
   box user (never `/opt`), then `clean-home.sh`.

Each phase echoes `--- <phase> <TOOL_NAME> ---` so a failure names the module
that caused it.

## Naming bands

Modules are sourced in `LC_ALL=C` filename order — the two-digit prefix is
purely a position, not a dependency graph:

| Band | Meaning |
|---|---|
| `0x` | base/apt (base package list, ssh config, user creation) |
| `1x` | languages & runtimes (node, go, rust, bun, uv) |
| `2x` | agent harnesses (claude-code, crush, opencode, pi, herdr) — `23-pi.sh` also owns pi's MCP adapter plumbing (see below), since it's pi-specific glue, not a standalone MCP server |
| `3x` | standalone binaries (zoxide, fzf, yq, shfmt, eza, git-delta) |
| `4x` | MCP servers (context7-mcp, playwright-mcp) |

## Helpers (`tools/lib.sh`)

`lib.sh` is the single source of truth for cross-cutting logic that would
otherwise be duplicated per module:

- `yb_arch_deb` — architecture as apt sees it (`amd64`/`arm64`)
- `yb_arch_go` — architecture as Go release tarballs name it
- `yb_arch_rust` — architecture as Rust target triples name it
- `yb_arch_node` — architecture as Node/`process.arch`-style release assets
  name it (`x64`/`arm64`)
- `yb_fetch_bin <url> <dest-name>` — download one static binary into
  `/usr/local/bin` and `chmod 0755` it
- `yb_fetch_tar_bin <url> <bin-name>...` — download a `.tar.gz` whose payload
  is bare executable(s) at the archive root straight into `/usr/local/bin`,
  and `chmod 0755` each named binary
- `yb_apt_repo <name> <key-url> <deb-line>` — register a third-party apt repo:
  dearmor a key into `/etc/apt/keyrings/<name>.gpg`, write
  `/etc/apt/sources.list.d/<name>.list`

**Modules must use these helpers rather than calling `dpkg`/`gpg`/`curl`
directly.** The reason is stricter than style: `tools/build.sh` sources every
module **on macOS** too, to regenerate the Dockerfile's `ENV` region. `dpkg`
does not exist on macOS, so `yb_arch_deb()` falls back to `uname -m` when it
is absent — a module that called `dpkg` directly would break that host-side
sourcing.

## Modules are sourced, not executed

- They run in the current shell, so they can set variables and register
  functions.
- They must be **idempotent** — apt is idempotent, `yb_fetch_bin` overwrites,
  etc.
- They must not rely on a previous module's state: `yb_each_module` (the
  iteration chokepoint in `tools/lib.sh`, used by `install-all.sh`,
  `tools/smoke.sh`, and `mcp/render.sh`) unsets
  every `TOOL_*` variable via `${!TOOL_*}` and every contract function
  (`tool_prepare`, `tool_apt_repo`, `tool_install`, `tool_configure`,
  `tool_seed`) before sourcing the next module, so a module can only see what
  it itself sets. **A shared constant cannot live at module scope** — if two
  modules need the same value, put it in `tools/lib.sh` instead (this bit the
  pre-refactor `pi-mcp-adapter` split, which is why it's now folded into
  `23-pi.sh` rather than living in its own file).

## Rules a module author will otherwise get wrong

These are real traps that bit during implementation of the module contract —
each one produces output that *looks* correct until you check closely.

1. **The `@HOME@` placeholder — never `$HOME`/`${HOME}` in `TOOL_ENV` or
   `TOOL_RUNTIME_ENV`.** Modules are sourced not only inside the build, but
   also on the **macOS host** by `tools/build.sh`, to generate the
   Dockerfile's `ENV` region. On the host, `$HOME` is the developer's own
   home directory — a double-quoted
   `"$HOME/.cargo"` would bake `/Users/<you>/.cargo` straight into the
   generated Dockerfile. Write the literal string `@HOME@` instead; each
   consumer substitutes it at its own point of use (the Dockerfile generator
   expands it against the box user's home, the in-image drift guard expands
   it against the live `$HOME`, the launcher expands it against the
   container's home when emitting `-e` pairs for `TOOL_RUNTIME_ENV`).

   **`TOOL_HOME_DIRS` is the one exception — do NOT prefix it with `@HOME@`.**
   Its entries are already bare-relative (`go/pkg/mod`, `.cargo/registry`)
   because `yolobox-init` prefixes `$HOME` itself when creating them. Writing
   `@HOME@/go/pkg/mod` there would end up creating `$HOME/@HOME@/go/pkg/mod`.

2. **`TOOL_MCP_JQ` must be single-quoted.** These filters legitimately contain
   `$schema` (crush, opencode need the JSON Schema key). In a *double-quoted*
   bash string, `$schema` is a shell variable reference that expands to
   empty **before `jq` ever runs** — the module ends up defining a filter
   equivalent to `{"": "https://…", ...}`. This is **valid JSON**, so no
   structural check (`jq -e .`, "contains every server") catches it; only a
   check for the literal key name does. Single-quoting keeps the `$schema`
   text intact for `jq` to interpret.

   The same trusted-input hazard applies to what these filters may ever
   *emit*: **crush treats its config file as trusted code and executes
   `$(...)` (command substitution) at load.** A filter — or any fragment it
   reads from — that produces `$(...)` anywhere in the rendered output would
   run arbitrary shell the moment crush parses its config. Never construct a
   filter that could echo command substitution through untouched.

   Also note: `timeout` is deliberately omitted from every harness's MCP
   filter. crush's `timeout` field is in seconds, opencode's is in
   milliseconds — no single value baked into the canonical fragment could be
   correct for both, so no filter emits the key at all, and every harness
   falls back to its own default.

3. **`tool_seed()` may not reference module-scope variables — literals only.**
   Unlike every other function in the contract, `tool_seed()`'s body is not
   run in a context where its module has been sourced. At build time,
   `install-all.sh` extracts it verbatim via `declare -f`, renames it to
   `tool_seed_<module>`, and writes the renamed definition plus one call line
   into `/opt/yolobox/manifest/seed.sh`. At every launch, `yolobox-init`
   sources that generated file directly — **without ever sourcing the
   original module** — so `TOOL_NAME`, `TOOL_MCP_PATH`, or any other
   module-scope variable or constant is simply unset in that context. Only
   literals and the function's own locals survive the trip; see the comment
   in `23-pi.sh`'s `tool_seed()` for a worked example of a value that looks
   like it should be shared with a module-scope constant but is deliberately
   duplicated as a literal instead.

4. **The generated Dockerfile `ENV` region, and why it's one instruction per
   variable.** `tools/build.sh` collects every module's `TOOL_ENV` and
   `TOOL_PATH` and writes them into a delimited, generated region of the
   Dockerfile; `tools/install-all.sh` then re-derives the same values at
   build time and fails the build if the live environment disagrees with
   what the modules declare (the drift guard). The generator emits **exactly
   one `ENV` instruction per variable**, never a multi-pair `ENV` with one
   variable's value built from another. This is not stylistic: per [Docker's
   own reference](https://docs.docker.com/reference/dockerfile/#environment-replacement),
   environment-variable substitution "use[s] the same value for each
   variable throughout the entire instruction" — a single `ENV` instruction
   containing both `FOO=bar` and `BAZ=${FOO}/baz` resolves `BAZ` against
   `FOO`'s value *before* this instruction ran, not the `bar` just set on the
   same line. That failure mode produces no error and no failed layer; it
   just silently bakes in the wrong value.

## Worked example (`tools/32-yq.sh`)

```sh
#!/usr/bin/env bash
# yq — single static binary from GitHub releases.
TOOL_NAME=yq
TOOL_VERSION=4.44.3
TOOL_SOURCES=( "https://github.com/mikefarah/yq/releases/download/v${TOOL_VERSION}/yq_linux_$(yb_arch_deb)" )
TOOL_SMOKE=( "yq --version" )

tool_install() {
    yb_fetch_bin \
        "https://github.com/mikefarah/yq/releases/download/v${TOOL_VERSION}/yq_linux_$(yb_arch_deb)" \
        yq
}
```

Everything about `yq` — its version pin, where the build fetches it from, how the
build doctor probes it, and how the smoke stage proves it works — lives in this
one file.

## Two gotchas a new module will hit

1. **`.dockerignore` is an allowlist.** It ignores everything (`*`), then `!`
   lines re-include specific paths. `tools/` itself is already re-included, but if a module's
   `tool_install()` needs the `COPY` to bring in some *other* new path (not just
   another `tools/*.sh` file), that path needs its own explicit `!` re-include or
   `COPY` will silently omit it.
2. **`.shellcheckrc` disables `SC2034`** ("appears unused"). Module metadata
   variables (`TOOL_NAME`, `TOOL_VERSION`, `TOOL_APT`, `TOOL_SOURCES`,
   `TOOL_SMOKE`) are read by `install-all.sh` **after** the module is sourced —
   shellcheck analyzes each file in isolation and cannot see that consumer, so
   without the rule disabled every module lints as ~1-5 false positives (~19
   across the whole library). Do not silence it per-line; the repo-wide
   `.shellcheckrc` is the intended fix.
