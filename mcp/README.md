# mcp/ — the MCP fragment library

To add an MCP server: drop one `mcp/<name>.json` fragment here, and — if the
server needs a binary that isn't already installed — one `tools/4x-<name>.sh`
module (see `tools/README.md`). That's it. `mcp/render.sh` merges every fragment
at build time into per-harness configs, and all four harnesses (Claude Code,
opencode, crush, pi) pick the new server up automatically. No harness-specific
wiring, no per-tool code anywhere else.

## Fragment shape

Each fragment is a single-key JSON object, in canonical Claude
`mcpServers`-entry form: `type`, `command`, `args`, and an optional `env`.
Example, `mcp/context7.json`:

```json
{
  "context7": {
    "type": "stdio",
    "command": "/usr/local/bin/context7-mcp",
    "args": []
  }
}
```

`render.sh` reduces every `mcp/*.json` fragment into one map and re-shapes it
per harness. A build fails if any fragment is invalid JSON.

## `render.sh` is generic — it names no harness

`render.sh` itself contains no mention of Claude Code, crush, opencode, or pi
(a build gate enforces this). It doesn't know how many harnesses there are or
what any of them are called. What it does, mechanically, per module:

1. Validate and merge every `mcp/*.json` fragment into one canonical map.
2. Walk every `tools/NN-name.sh` module (via `tools/lib.sh`'s
   `yb_each_module`) looking for one that declares `TOOL_MCP_PATH`.
3. For each one it finds: run the canonical map through that module's
   `TOOL_MCP_JQ` filter, write the result to `TOOL_MCP_PATH`, `chmod` it to
   `TOOL_MCP_MODE` (default `0444`), and record the path plus its top-level
   key in a manifest that `mcp/smoke.sh` reads later (it runs unprivileged
   and can't source modules itself).

**A harness's config path, its schema reshape, and its file mode are not
`render.sh`'s concern — they live entirely in that harness's owning
`tools/2x-name.sh` module.** To see what Claude Code's rendered config looks
like, read `20-claude-code.sh`'s `TOOL_MCP_PATH`/`TOOL_MCP_JQ`/`TOOL_MCP_MODE`,
not `render.sh`. This mirrors the tools/ contract itself: adding a fifth
harness is a matter of giving its module those three variables, not touching
the renderer.

## What gets emitted, and where each harness reads it

| Output | Harness | How the harness finds it |
|---|---|---|
| `/etc/claude-code/managed-mcp.json` | Claude Code | Fixed managed-config path outside `$HOME`; Claude Code loads *only* the servers this file defines. Declared by `20-claude-code.sh` |
| `/opt/yolobox/mcp/opencode.json` | opencode | `ENV OPENCODE_CONFIG=/opt/yolobox/mcp/opencode.json`, declared alongside the MCP facet in `22-opencode.sh` |
| `/opt/yolobox/mcp/crush.json` | crush | `ENV CRUSH_GLOBAL_CONFIG=/opt/yolobox/mcp/crush.json`, declared alongside the MCP facet in `21-crush.sh` |
| `/opt/yolobox/mcp/mcp.json` | pi | pi has no native MCP support; the `pi-mcp-adapter` extension (pi plumbing owned by `23-pi.sh`, not a standalone MCP server itself) reads `~/.config/mcp/mcp.json`, which `yolobox-init` symlinks to this file (copy-once, never overwriting a user edit) |

## Schema differences each harness's module normalises

The canonical fragment form (above) is Claude's — its module's `TOOL_MCP_JQ`
is close to the identity filter (`{mcpServers: .}`). Each other harness's own
module reshapes the same canonical map differently:

- **opencode** (`22-opencode.sh`) uses top-level key `mcp`, and each entry
  becomes `{"type":"local","command":([.command]+(.args//[])),"environment":(.env//{}),"enabled":true}`
  — note `command` becomes a **single argv array** (command + args combined),
  and the env key is `environment`, not `env`.
- **crush** (`21-crush.sh`) uses top-level key `mcp`, and each entry is
  `{"type":"stdio","command":...,"args":...,"env":...}` — a near 1:1 match to
  the canonical fragment.
- **`timeout` is omitted everywhere.** crush's `timeout` is in seconds,
  opencode's is in milliseconds — no single value is correct for both, so no
  module's `TOOL_MCP_JQ` emits the key at all, and every harness falls back
  to its own default.

Every one of these filters must be **single-quoted** in its module (not
double-quoted): a double-quoted filter containing `$schema` silently expands
to an empty string before `jq` ever runs, producing valid-but-wrong JSON that
no structural check catches. Also, never let a filter emit `$(...)` —
crush treats its config as trusted code and executes command substitution at
load.

## Two hard rules

1. **Never use `npx` in a fragment.** `npx`'s on-demand resolution means the
   exact server version, and whether it starts at all, depends on whatever the
   registry serves at container-start time — not what was pinned and tested at
   build time. Every server is instead installed globally at build time (via
   its `tools/4x-*.sh` module) and referenced by **absolute path** under
   `/usr/local/bin`, so the version is pinned, reproducible, and has no
   network dependency at server start.
2. **Never emit `$(...)` (command substitution) into a rendered config.**
   crush executes its config file as trusted code at load time. A fragment (or
   `render.sh`'s transform of it) that produces `$(...)` anywhere in the output
   would run arbitrary shell inside crush's config parser.

## Security note — config integrity, not a full security boundary

The rendered configs are root-owned (`managed-mcp.json`, mode `0644`, under
the root-owned `/etc`; the `/opt/yolobox/mcp/*.json` set, mode `0444`, under a
root-owned `/opt`) — the box user cannot edit, delete, or replace them, and cannot even
`mv` the parent `/opt/yolobox` or `/opt/yolobox/mcp` directory aside to swap in
its own, because `/opt` itself stays root-owned (unlike `/usr/local`, which the
box user does own). So the box cannot change **which servers are configured**.

But the binaries those configs point at — `/usr/local/bin/context7-mcp`,
`/usr/local/bin/playwright-mcp` — live under `/usr/local`, which the box user
*does* own by design (that's what makes ad-hoc `npm -g`/`cargo install`/`go
install` work). **Binary integrity is therefore not guaranteed**: the box user
can replace `context7-mcp` or `playwright-mcp` with something else and the
rendered config will still happily point at it. Do not describe the rendered
configs as a security boundary — they enforce *which servers exist and how
they're invoked*, not *what those binaries actually do*.

## `claude mcp add` does not work inside the box

Deploying `/etc/claude-code/managed-mcp.json` puts Claude Code's MCP
configuration under exclusive managed control. Once it's present, `claude mcp
add` fails with an error to that effect — this is accepted, not a bug: the
managed config is the whole point of baking Context7 and Playwright in for
every box, and letting `claude mcp add` silently coexist with it would just
reintroduce per-box drift.
