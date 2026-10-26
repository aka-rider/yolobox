# Harnesses: vendor installs, and the one path the box owns

claude, pi, opencode and agent-browser are not nixpkgs packages. They install
into the agent's home from their own vendors and update themselves there, so
the box gives up owning their versions. What it owns instead is one path,
`~/.local/bin/claude`. This file tells why, and how to recognise each failure
that has no error message. The mechanisms are in the comments of
`harnesses.nix`.

## The launcher

Claude's settings reach it only as `--settings <file>`. Claude Code builds
`policySettings` from three tiers (remote, MDM, `/etc/claude-code/managed-settings.json`)
and takes only the first non-empty one, with no merge. This account's
`~/.claude/remote-settings.json` is never empty, so the `/etc` tier is
discarded with nothing logged. `--settings` is the one channel that survives
(see [Output directory and the PreToolUse hook](display.md#output-directory-and-the-pretooluse-hook)
for what the file carries). So whatever puts that flag on the command line has
to be the `claude` a human session actually runs.

### How the box lost that path twice

Both attempts raced a directory the box does not own, and then forbade
anything from living in it.

- `d79fea0` put a nix wrapper on the system PATH, pointed it at upstream's
  self-updating install and appended `~/.local/bin` to PATH. It assumed only
  `environment.localBinInPath` could prepend that directory. Within a day this
  proved wrong: the agent's own shell rc files may prepend it too, and `yo enter` ends
  with `exec "$SHELL" -l`, an interactive login zsh that sources them. The
  first `claude update` (2026-09-04 14:42, 2.1.259 to 2.1.260) put a bare
  launcher ahead of the wrapper and ended herd reporting for every typed
  `claude`, box-wide.
- `279184d` answered by making the box the only claude installation, with
  `DISABLE_AUTOUPDATER`, a reaper that deleted any home install within a
  second, and a rule that no second claude may exist.

The box now owns the path itself. `~/.local/bin/claude` is a tmpfiles `L+`
link to `/etc/yolobox/bin/claude`, a launcher that picks the newest binary
under `~/.local/share/claude/versions/` and execs it with `--settings
/etc/claude-code/claude-hooks.json` ahead of `"$@"`, so a user's own later
`--settings` still wins. Anthropic documents (setup docs, since 2.1.207) that a
custom launcher at that path is left alone by `claude update` and auto-update,
which only drop new binaries into `versions/`. Shell rc files may prepend
`~/.local/bin` all they like. The first `claude` on PATH is the box's launcher
either way, and `claude update` works and is welcome. `DISABLE_AUTOUPDATER`
and the reaper are gone.

### Details that matter for a launcher in front of every session

- It is a `writeShellApplication` with no `runtimeInputs`. Those would
  prepend the store's coreutils, findutils and git to `PATH`, and since the
  script `exec`s claude, every tool the project runs inside the session (its
  own pinned `git`, a `find` from its devbox) would be shadowed by the box's.
  Nobody would connect that difference to a launcher. The script names `find`,
  `sort`, `tail`, `mkdir` and `git` by store path.
- It skips a binary modified in the last minute when another exists, because
  claude writes a new version straight into `versions/` and the newest file
  can be half-downloaded. A lone binary, as on a first install, is used
  anyway, since the alternative is no claude for a minute.

### claude's own "Fix" warning

Since about 2.1.29x, `claude update` and `claude upgrade` print "Warning:
/home/agent/.local/bin/claude was not created by the native installer (it is
not a symlink into the versions/ directory), so auto-update leaves it
untouched." A "Fix:" line follows, saying automatic version cleanup is
disabled and that removing `~/.local/bin/claude` and running `claude update`
lets Claude Code manage the launcher again. This is expected. The installer
noticed the box's launcher, and the cleanup it gave up is what the version
keeper does instead. Never follow the "Fix". Removing the link hands the path
back to the installer, and the next `claude` runs unwrapped, with no
`HERDR_AGENT` and no Playwright hook, until the path unit or tmpfiles
re-links it.

## The version keeper

The custom launcher costs one thing. Claude prunes old versions only when it
owns the launcher, and each version is about 330 MB. So `yolobox-claude-launcher`,
the user path unit that guards the link, also prunes `versions/` to the two
newest. It watches `~/.local/bin` and `versions/`, re-links within a second
when `readlink` disagrees, and writes only when something is wrong, so the
write it causes settles instead of looping. The tmpfiles rules at boot and on
`switch` are the backstop for the window the path unit cannot see, an install
made while no user manager of the agent's was running.

Three things about it were learned by getting them wrong:

- **Rank first, settle second.** The keeper ranks all versions, so a fresh
  download counts toward the two kept, and only then deletes older ones that
  have been untouched for more than ten minutes. Filtering by settledness
  before ranking kept a third version around: the fresh one was not a
  candidate, so it displaced nobody, and the settled ones were pruned down to
  two without it.
- **An hourly timer.** A path unit fires only on a change, and a version too
  fresh to prune becomes prunable with nothing left to change. The
  `yolobox-claude-launcher.timer` (`Persistent=true`, so a missed hour runs at
  the next login) runs the same service.
- **`StartLimitIntervalSec=0`.** The path unit fires on every write into the
  version store, and one ~330 MB download is thousands of them. Systemd's
  default limit (five starts in ten seconds) trips in the first second, and the
  tripped service leaves the path unit failed too, which stops it watching
  until something resets it. The work per start is a few `readlink`s and a
  `find`, so no limit is the right limit.

Check that pruning is alive with `ls ~/.local/share/claude/versions` as the
agent. At most two entries should be there.

## Install phases

`yolobox-harness-install` is a user oneshot gated `ConditionUser=agent` and
wanted by `default.target`, so it runs at boot rather than at the first `yo
enter`. Each phase is idempotent and checks before it acts. The commands and
their guards are in `harnesses.nix`. What needs recording here:

- **No vendor installer edits an rc file.** pi's and opencode's run with the
  defaults or with `--no-modify-path`.
- **A failed phase does not stop the others.** The script runs claude, pi,
  agent-browser, the pi packages, the browser config and opencode as separate
  phases, each in its own errexit subshell through `phase()`. A failed phase
  goes into `failed`, the script moves on, and it prints every failure and
  exits 1 at the very end, so `Restart=on-failure` still fires. A down vendor
  host or a bad config file no longer keeps unrelated installs from happening.
  The final `--version` checks are phases of their own for the same reason.
  A phase that depends on another (the pi packages need pi) fails again on
  its own when its dependency failed, so one outage can list more than one
  phase.
- **The pi probe uses the launcher's own path.** The check is
  `~/.pi/agent/bin/pi` being executable, not `command -v pi`. An install that
  is off PATH would otherwise be reinstalled on every boot, which was the
  exact state after a hand-run migration on 2026-10-10.
- **Probes written for `pipefail`.** `cmd | grep -q x` is a trap under it.
  `grep -q` exits at its first match, a producer still writing dies of
  SIGPIPE, and `pipefail` turns that into a failed probe. An installed
  package then reads as missing and the `|| install` after it reruns at
  random on any boot. `probe_has` captures the producer's whole output first
  and greps the captured text.
- **The agent-browser probe runs the binary.** `agent-browser --version`
  rather than `command -v agent-browser`, because a binary on PATH whose
  postinstall download never ran is present and broken, and only running it
  tells the two apart. It installs with scripts on, since the postinstall is
  the download, and the download fails silently, so the `--version` call is a
  hard check.
- **The browser config merge is loud.** The `pi-agent-browser-native` config
  file is merged with `jq`. A file that is not a JSON object (empty,
  unparseable, or valid `null` or `false`) fails that phase naming the file,
  because the file also holds the extension's own settings and overwriting it
  with `{}` would lose them. An unchanged file is not rewritten on every boot.
- **`NPM_CONFIG_PREFIX=$HOME/.local`** puts every `npm -g` binary (agent-browser)
  in `~/.local/bin` beside the launcher. pi's own `pi install` packages live
  under `~/.pi/agent/npm/`, not there. `environment.localBinInPath` puts that
  directory on PATH for non-login shells too, which every non-interactive `yo`
  guest call (`ssh_run`, behind `yo gc`, `yo status` and the rest) run in.

## pi

pi is the one phase that used to be `npm install -g` and no longer is.
`~/.pi/agent/bin/pi` is a POSIX sh launcher that execs
`~/.pi/agent/install/releases/<version>/node_modules/.bin/pi`, a `#!/usr/bin/env
node` script. The box's nixpkgs node runs it (pi needs node >= 22.19) and
nix-ld is not involved. Every prompt in pi's `install.sh` reads `/dev/tty`,
and under systemd there is none, so it takes the defaults.

The installer's own npm-to-managed migration never fires here. By the time
the install service runs, the boot-time `L+` rule has already replaced an npm
pi's `~/.local/bin/pi` with a link to the not-yet-installed launcher, so the
installer sees no pi and installs fresh. The script therefore removes
`@earendil-works/pi-coding-agent` with `npm uninstall -g` itself whenever its
package directory is still under `~/.local/lib/node_modules`. It also needs
`awk`, which the unit's `path` carries.

The box makes pi reachable with a link, `~/.local/bin/pi` to
`~/.pi/agent/bin/pi`, asserted by the install script on every run and by a
tmpfiles `L+` rule at boot and on `switch`, the same pattern as opencode.
`pi update` self-updates and keeps only two releases (since pi 1.1.0), so
unlike claude nothing here prunes.

Run by hand in a tty, the installer asks "Add /home/agent/.pi/agent/bin to
your PATH in /home/agent/.zshrc now? [Y/n]". Answer `n`. The box's link already
puts pi on PATH, and a yes is the rc-file edit the box forbids. The text "This
Pi was installed with npm. Pi now uses a managed installation..." comes from
`install.sh`, not from pi, and `pi update` on an npm install only recommends
migrating.

### pi's settings.json belongs to pi

pi rewrites `~/.pi/agent/settings.json` on its own and logs, rather than
raises, a failed write. A deleted `nix/pi.nix` (`cb122e1`) once linked it to
`/etc/static/...`. The module went and the dangling link stayed. That file is
the only place pi keeps its `packages` list, so no
package listed there installed: `pi list` said "No packages
installed" and `cc-compat` complained on every start that `AskUserQuestion` was
unavailable.

Recognise it with `ls -l ~/.pi/agent/settings.json`. A symlink is the defect
and `rm` is the fix. No tmpfiles rule can do this, because there is no "delete
only if dangling" and an unconditional `r` would delete the real file.

### What cannot install

The VM has no C or Python toolchain, so an extension that pulls a native module
without an aarch64 prebuild cannot install. That dropped
`@plannotator/pi-extension`, whose `node-pty` prebuilds exist for darwin and
win32 only. Prebuilt glibc packages like `@ast-grep/napi` are fine.

### Ownership and retired links

The flake owns nothing in pi's extensions tree. It used to render
`yolobox-agent-state.js`, pi's herd-report source under the old design. pi's
own herdr integration (`~/.pi/agent/extensions/herdr-agent-state.ts`) replaced
it, and the integration is installed with `herdr integration install pi`, not
rendered by the flake. `pi-mcp-adapter` and the `mcp-scripting` skill went
earlier, with `nix/mcp.nix`. The context7 and agent-browser packages are
installed with `pi install`, so pi owns them. Skills, agents, prompts,
`cc-compat` and the rest of the packages are the agent's own to install. Two extensions
registering the same tool name kill pi at startup, which is what retired the
flake's pi-lsp extension in favour of pi-lens.

Every one of those retirements needed an `r` rule, and one did not get it the
first time. Dropping the `environment.etc` entry for `yolobox-agent-state.js`
without an `r` rule left a dangling symlink, because a dropped `L+` link is
not removed by the rebuild that drops it. Recognise it with `ls -l`, the same
way as pi's `settings.json`. Delete such a link by hand, or give it an `r`
rule.

## Plugins, not MCP files

`nix/mcp.nix` is gone, and with it the three per-harness config files it
rendered, `yolobox-mcp-smoke`, the per-engine playwright wrappers, the
nixpkgs `playwright-mcp` and `context7-mcp` packages, the nix-built
`pi-mcp-adapter` with its `mcp-scripting` skill, and `~/.config/mcp/mcp.json`.
Declaring servers once and rendering them per harness was correct while every
harness needed a file. It stopped being correct once each vendor grew its own
registry, because the rendered files then compete with the registry instead of
feeding it.

claude gets two plugins from the official marketplace. `yolobox-harness-install`
installs them with `--scope user`, after registering
`anthropics/claude-plugins-official`, which a non-interactive box requires
first. They are recorded in `~/.claude/settings.json` under `enabledPlugins`.

- `context7@claude-plugins-official` is a remote HTTP MCP with no local
  runtime.
- `playwright@claude-plugins-official` runs `npx @playwright/mcp@latest`, so
  it wants network the first time each version is used.

Because that file records the installs, `~/.claude/settings.json` must not be
a symlink into a version-controlled directory, or plugin installs write there instead.

pi gets no MCP at all. `@upstash/context7-pi` is Upstash's own pi package,
pure JS, exposing native pi tools. `pi-agent-browser-native` drives Vercel's
`agent-browser` CLI the same way. Both go in with `pi install`, never by
editing pi's settings file. The extension does not read
`AGENT_BROWSER_EXECUTABLE_PATH` itself, so the install service writes
`~/.pi/config/pi-agent-browser-native/config.json` (`browser.executablePath`)
with `jq`. `pi install` does not put the extension's own
`pi-agent-browser-config` helper on PATH.

opencode keeps only its LSP entry, which `nix/lsp.nix` renders to
`/etc/yolobox/lsp/opencode.json` and names through `OPENCODE_CONFIG`.

## Lingering

The agent account lingers (`users.users.agent.linger`). That is what makes
"at boot" true for everything above. Without lingering the agent's user
manager starts at the first login and stops at the last logout. The install
service, the launcher path unit, the podman prune timer and the herdr server
would all wait for a session, and `/run/user/1000` would come and go under
anything that kept a socket there. With lingering they run from boot and that
runtime directory persists.

## Recognising a broken launcher

There is no error anywhere. An unwrapped claude works perfectly and carries
neither `HERDR_AGENT` nor the Playwright hook. The probe is to ask the two
shells and follow the link:

```sh
zsh -lic 'command -v claude'         # what a human session gets
bash -lc 'command -v claude'         # what an ssh command gets
readlink -f ~/.local/bin/claude      # where the link actually ends
```

The first two must both answer `/home/agent/.local/bin/claude`, and the third
must answer `/etc/yolobox/bin/claude`. During the 2026-09-04 incident the two
shells disagreed. The old `yo herd-check`, removed with the rest of the
host-forwarded herd wiring (see [Machine panes](herd-server.md#machine-panes)),
missed it throughout. Its guest half ran over a non-interactive, non-login
shell that never sources `~/.zshrc`, so it measured a shell nobody uses. This
probe needs no herd wiring, which is why it is the one worth keeping.

## herdr's pi integration

pi's own herdr integration writes `~/.pi/agent/extensions/herdr-agent-state.ts`
(`herdr integration install pi`, run by `yolobox-harness-install`). It is a
vendor-owned file, not one the flake renders. An earlier design needed
`yolobox:pi` to be pi's only source into a Mac-side herd, so a tmpfiles `r`
rule deleted that exact path on every boot and switch. That need is gone, and
so is the rule. Never re-add a tmpfiles rule that deletes a herdr-installed
integration file. `systemd-tmpfiles-resetup.service` reapplies `r` rules on
every switch, so the rule would uninstall the integration at the next switch.
The symptom, pi missing from the herd with no error, would look exactly like
an integration that never installed.

The install used to be guarded by `[ -f ~/.pi/agent/extensions/herdr-agent-state.ts ]`.
Once the file existed, nothing touched it again. The herdr 0.9.0 to 0.9.1 bump
moved the integration from v8 to v9, and a box installed before the bump kept
running v8 silently, with `herdr integration status` reporting `pi: outdated`
the whole time and nothing reading that output. The guard is now `herdr
integration status | grep -q '^pi: current' || herdr integration install pi`,
so a version bump is picked up on the next run of the install service, without
deleting the extensions file by hand.
