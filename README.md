# yolobox

A Docker **blast-radius devbox** for running AI coding agents (Claude Code, opencode,
Crush, pi) against **one** project directory, with your host's passwd, secrets, and
home directory withheld — while still letting agents show up in host
[herdr](https://herdr.dev) and `git push` through the host **1Password SSH agent**
with no private key ever entering the box.

> **"Airgapped" here means credential and host isolation, not no-network.** Agents get
> outbound HTTPS; they don't get your shell environment, your real `~`, your SSH keys,
> or root.

---

## Isolation model

| Withheld from the box | Given to the box |
|---|---|
| Your real host `~` / home directory | One project dir, mounted read-write at `/work` |
| `/etc/shadow`, host passwd entries | Your uid:gid (files you create are owned by you on the host) |
| `SECRET_*`, `*_TOKEN`, `*_KEY`, `*_PASSWORD` env, including any agent API key | A tiny env allowlist: `TERM`, `COLORTERM`, `LANG`, plus herdr/ssh vars |
| root / sudo / new privileges (`--cap-drop=ALL`, `no-new-privileges`) | Non-root user, **your host username**, login zsh |
| Any private SSH key | 1Password **agent socket** only (protocol crosses, key stays in 1Password) |
| Your host's package caches / config | Its own `/usr/local`, writable and yours to install into (`/opt` stays root-owned) |

One container per workdir, persistent across runs. `HOME` lives on a dedicated
Docker named volume, `yolobox-home` — not your real `~` — so the box keeps its own
agent logins and caches across runs *and* container recreation. `--stop` stops it,
`--destroy` removes it (home volume untouched), `--diff` shows what changed,
`--list` lists every box.

---

## Quickstart

```bash
# Drop into a login shell in the box, pointed at a project:
./yolobox -w ~/Developer/myproject

# Run a one-shot agent instead of an interactive shell:
./yolobox -w ~/Developer/myproject -- claude

# Force a rebuild of the image (e.g. after editing tools/ or the Dockerfile):
./yolobox -w ~/Developer/myproject --build

# See what an agent installed ad hoc, then tear the container down:
./yolobox -w ~/Developer/myproject --diff
./yolobox -w ~/Developer/myproject --destroy
```

Install something ad hoc inside the box (`npm install -g`, `cargo install`, …), use
`--diff` from the host to see what changed, then promote anything worth keeping into
a `tools/NN-name.sh` module (see `tools/README.md`) so it survives `--destroy`.

### Flags

| Flag | Meaning |
|---|---|
| `-w <dir>` | **Mandatory** (except `--list`). Mounted rw at `/work`. Must be under a Docker-shared root. |
| `--ssh` | Forward the host 1Password SSH agent (see below). |
| `--build` | Rebuild `yolobox:local` even if it already exists. |
| `--yes` | Recreate an out-of-date box without the confirmation prompt. |
| `--keep` | Abort instead of recreating an out-of-date box. |
| `--stop` | Stop the box's container and exit. |
| `--destroy` | Print `docker diff`, then remove the box's container and exit. Confirms unless `--yes`. |
| `--diff` | Print `docker diff` for the box's container and exit. |
| `--list` | List every yolobox container and exit. Doesn't need `-w`. |
| `-- <cmd...>` | Command to run instead of the default handoff. |

With no `--`, the box starts **herdr** — its own multiplexer, using your seeded
config — or a login shell if there's no TTY or you launched from a host herd pane
(herd refuses to nest).

No API keys are ever forwarded. Log agents in inside the box; credentials persist
in the `yolobox-home` volume across runs. `YOLOBOX_HOME_VOLUME` overrides the
volume name (default `yolobox-home`) — use a distinct one for an untrusted run.

---

## Requirements

- **Docker Desktop for macOS.**
- **Docker file sharing** must cover your project dirs (Settings → Resources → File
  Sharing) — yolobox hard-fails `-w` with a clear message otherwise.
- **1Password** with the SSH agent enabled, for `--ssh` (one-time host setup below).

### 1Password one-time host setup

Needs a full Docker Desktop restart. Docker Desktop is launched by `launchd` and
ignores your shell environment, so its SSH bridge only reaches 1Password if you
point `launchctl`'s `SSH_AUTH_SOCK` at the 1Password agent socket **and then fully
restart Docker Desktop**:

```bash
launchctl setenv SSH_AUTH_SOCK "$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
# then: fully quit and reopen Docker Desktop (not just a window — the whole app)
```

Persist it across reboots with a LaunchAgent.

**Touch ID:** expect one approval per session the first time a key is used. Set a
time-window grant in 1Password to avoid repeated prompts.

**Commit signing:** set `gpg.format=ssh` and `user.signingkey=<literal public key>`
in the box, and remove any `op-ssh-sign` program line — the forwarded agent signs
directly.

---

## herdr integration

The box runs an unprivileged, loopback-only sshd; the **host** opens an *outbound*
reverse tunnel into it, authenticated by your 1Password key — the host never
listens. Claude Code hooks baked into the image report through that tunnel under
the source `yolobox:claude`, so `herdr agent list` shows the box's claude live
beside your other agents.

Bind a key so a pane launches yolobox with the right env automatically:

```toml
[[keys.command]]
key = "prefix+alt+y"
type = "pane"
command = "/path/to/yolobox -w . -- claude"
```

`herdr:claude` is reserved for herd's own process detection, which a container
never satisfies (its foreground process is `docker`) — reporting under
`yolobox:claude` instead is what makes the boxed agent's status show up at all.

---

## Tools & MCP

Everything installable in the image — a CLI tool, a runtime, an agent harness, an
MCP server — is a single self-describing shell module: one `tools/NN-name.sh` file
per tool, one `mcp/<name>.json` fragment per server. Adding either is a one-file
change. Full contracts: [`tools/README.md`](tools/README.md) and
[`mcp/README.md`](mcp/README.md).

Not every harness can report its agent's live state into herdr — each harness
module declares its own support (`TOOL_REPORT`), and the module is the source of
truth.

---

## What is NOT isolated

- **`/work` is read-write** — agents can modify, create, and delete anything there,
  and those changes are real on your host.
- **The `yolobox-home` volume is durable** — it carries the box's own agent logins
  and caches across runs and container recreation.
- **Network is full outbound** — agents can reach the internet.
- **`/usr/local` is writable** and survives until `--destroy` or config-driven
  recreation.
- **MCP config integrity, not binary integrity** — the rendered MCP configs are
  immutable to the box, but the binaries they point at live under the writable
  `/usr/local` and are not.
