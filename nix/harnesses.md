# Harnesses: vendor installs, and the one path the box owns

claude and pi are not nixpkgs packages. They install
into the agent's home from their own vendors and update themselves there, so
the box gives up owning their versions. What it owns instead is one path,
`~/.local/bin/claude`. This file tells why, and how to recognise each failure
that has no error message. The mechanisms are in the comments of
`harnesses.nix`.

## The launcher

The launcher exports `HERDR_AGENT=claude` before it runs claude. herdr
classifies a pane by its foreground process name, and the vendor binary is
named after its version directory (`2.1.268`, say), so herdr never recognises
an unwrapped claude. Whatever is typed as `claude` in a human session has to
be this launcher.

### Why the box owns that path

A wrapper beside a directory the box does not own loses the race for that
directory, and forbidding the vendor's updater to use it is not an option.

- A nix wrapper on the system PATH, pointed at upstream's self-updating
  install, with `~/.local/bin` appended to PATH, assumes only
  `environment.localBinInPath` can prepend that directory. The agent's own
  shell rc files may prepend it too, and `yo enter` ends with
  `exec "$SHELL" -l`, an interactive login zsh that sources them. The first
  claude self-update then puts a bare launcher ahead of the wrapper and herd
  reporting stops for every typed `claude`.
- Making the box the only claude installation would need `DISABLE_AUTOUPDATER`,
  a reaper that deletes any home install, and a rule that no second claude may
  exist. That forbids the vendor's own updater from using its own directory.

The box therefore owns the path itself. `~/.local/bin/claude` is a tmpfiles `L+`
link to `/etc/yolobox/bin/claude`, a launcher that picks the newest binary
under `~/.local/share/claude/versions/` and execs it with `HERDR_AGENT=claude` set. Anthropic documents (setup docs, since 2.1.207) that a
custom launcher at that path is left alone by `claude update` and auto-update,
which only drop new binaries into `versions/`. Shell rc files may prepend
`~/.local/bin` all they like. The first `claude` on PATH is the box's launcher
either way, and `claude update` works and is welcome.

### Details that matter for a launcher in front of every session

- It is a `writeShellApplication` with no `runtimeInputs`. Those would
  prepend the store's coreutils, findutils and git to `PATH`, and since the
  script `exec`s claude, every tool the project runs inside the session (its
  own pinned `git`, a `find` from its devbox) would be shadowed by the box's.
  Nobody would connect that difference to a launcher. The script names `find`,
  `sort` and `tail` by store path.
- It skips a binary modified in the last minute when another exists, because
  claude writes a new version straight into `versions/` and the newest file
  can be half-downloaded. A lone binary, as on a first install, is used
  anyway, since the alternative is no claude for a minute.

### claude's own "Fix" warning

`claude update` and `claude upgrade` print "Warning:
/home/agent/.local/bin/claude was not created by the native installer (it is
not a symlink into the versions/ directory), so auto-update leaves it
untouched." A "Fix:" line follows, saying automatic version cleanup is
disabled and that removing `~/.local/bin/claude` and running `claude update`
lets Claude Code manage the launcher again. This is expected. The installer
noticed the box's launcher, and the cleanup it gave up is what the version
keeper does instead. Never follow the "Fix". Removing the link hands the path
back to the installer, and the next `claude` runs unwrapped, with no
`HERDR_AGENT`, until the path unit or tmpfiles
re-links it.

## The version keeper

The custom launcher costs one thing. Claude prunes old versions only when it
owns the launcher, and each version is a large binary. So `yolobox-claude-launcher`,
the user path unit that guards the link, also prunes `versions/` to the two
newest. It watches `~/.local/bin` and `versions/`, re-links within a second
when `readlink` disagrees, and writes only when something is wrong, so the
write it causes settles instead of looping. The tmpfiles rules at boot and on
`switch` are the backstop for the window the path unit cannot see, an install
made while no user manager of the agent's was running.

Three properties it must keep:

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
  version store, and one large download is thousands of them. Systemd's
  default limit (five starts in ten seconds) trips in the first second, and the
  tripped service leaves the path unit failed too, which stops it watching
  until something resets it. The work per start is a few `readlink`s and a
  `find`, so no limit is the right limit.

Check that pruning is alive with `ls ~/.local/share/claude/versions` as the
agent. At most two entries should be there.

## Install phases

`yolobox-harness-install` is a user oneshot gated `ConditionUser=agent`. It
runs at boot rather than at the first `yo enter`, started by a path unit of the
same name once the network is up. Each phase is idempotent and checks before it
acts. The commands and their guards are in `harnesses.nix`. What needs recording
here:

- **The install waits for the network, by an event.** The agent's user manager
  starts at boot (lingering), in parallel with dhcpcd, and a user unit cannot
  order itself after a system unit. An install wanted by `default.target` ran
  at once, hit `curl: (6) Could not resolve host` for every vendor, and waited
  out `RestartSec` (20 s) before the retry that worked. A `claude --version`
  check run in that window failed. The fix has two halves.
  The system unit `yolobox-network-ready` wants and follows `dhcpcd.service`.
  dhcpcd runs with `waitip`, so it forks, and the unit counts as started, only
  after the lease is applied and resolvconf has written the resolver; the boot
  log shows `leased 192.168.5.15` a moment before `Started DHCP Client`. Nothing
  else activates `network-online.target` on this box, so the unit names dhcpcd
  itself. It is a `RemainAfterExit` oneshot that creates
  `/run/yolobox-network-ready` and removes it when stopped. The agent's path
  unit `yolobox-harness-install.path` has `PathExists` on that file, wanted by
  `default.target`; systemd watches it with inotify and starts the service when
  it appears. The service has no `wantedBy` of its own. `Restart=on-failure`
  stays, for a vendor that is down. Recognise a regression by
  `journalctl --user -u yolobox-harness-install` showing `Could not resolve
  host` at the first start after a boot.
- **Joining the install from a script.** The service is `Type=oneshot` with
  `RemainAfterExit`, so `sudo systemctl --user -M agent@ start
  yolobox-harness-install` returns when a run ends and exits non-zero when it
  failed. It joins a run already in progress, and it runs a unit that is
  waiting out `Restart=` at once. `tests/box-hardening-check.sh` does this
  before it checks the harness versions.

- **No vendor installer edits an rc file.** pi's runs with the defaults, since
  there is no tty under systemd to answer its prompts.
- **A failed phase does not stop the others.** The script runs claude, pi and
  herdr-pi as separate phases, each in its own errexit subshell through `phase()`. A failed phase
  goes into `failed`, the script moves on, and it prints every failure and
  exits 1 at the very end, so `Restart=on-failure` still fires. A down vendor
  host or a bad config file does not keep unrelated installs from happening.
  The final `--version` checks are phases of their own for the same reason.
  A phase that depends on another (herdr-pi needs pi) fails again on
  its own when its dependency failed, so one outage can list more than one
  phase.
- **The pi probe uses the launcher's own path.** The check is
  `~/.pi/agent/bin/pi` being executable, not `command -v pi`. An install that
  is off PATH would otherwise be reinstalled on every boot.
- **Probes written for `pipefail`.** `cmd | grep -q x` is a trap under it.
  `grep -q` exits at its first match, a producer still writing dies of
  SIGPIPE, and `pipefail` turns that into a failed probe. An installed
  package then reads as missing and the `|| install` after it reruns at
  random on any boot. `probe_has` captures the producer's whole output first
  and greps the captured text.
- **`NPM_CONFIG_PREFIX=$HOME/.local`** tells the one remaining `npm -g` call,
  the removal of pi's old npm package (see [pi](#pi)), where that package was
  installed.

## pi

pi installs through its vendor's `install.sh`, not `npm install -g`.
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
package directory is under `~/.local/lib/node_modules`. It also needs
`awk`, which the unit's `path` carries.

The box makes pi reachable with a link, `~/.local/bin/pi` to
`~/.pi/agent/bin/pi`, asserted by the install script on every run and by a
tmpfiles `L+` rule at boot and on `switch`, the same pattern as claude's launcher.
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
raises, a failed write. That file is the only place pi keeps its `packages`
list. If it is a dangling symlink (into `/etc/static/...`, left by a module
that no longer declares it), no package listed there
installs: `pi list` says "No packages installed" and extensions that depend on
an installed package complain on every start.

Recognise it with `ls -l ~/.pi/agent/settings.json`. A symlink is the defect
and `rm` is the fix. No tmpfiles rule can do this, because there is no "delete
only if dangling" and an unconditional `r` would delete the real file.

### What cannot install

The VM has no C or Python toolchain, so an extension that pulls a native module
without an aarch64 Linux prebuild cannot install, for example anything that
needs `node-pty`, whose prebuilds exist for darwin and win32 only. Prebuilt
glibc packages like `@ast-grep/napi` are fine.

### Ownership and retired links

The flake owns nothing in pi's extensions tree. pi's own herdr integration
(`~/.pi/agent/extensions/herdr-agent-state.ts`) is installed with `herdr
integration install pi`, not rendered by the flake.
Skills, agents, prompts and the rest of the packages are the agent's own to
install. Two extensions registering the same tool name kill pi at startup, so
the flake ships no LSP extension of its own; pi's LSP is the user's dotfiles
concern (see `nix/lsp.md`).

Retiring a link the flake renders needs an `r` rule. Dropping an
`environment.etc` entry alone leaves a dangling symlink, because a dropped
`L+` link is not removed by the rebuild that drops it. Recognise it with
`ls -l`, the same way as pi's `settings.json`. Delete such a link by hand, or
give it an `r` rule.

## Plugins, not MCP files

The box renders no per-harness MCP config files. Declaring servers once and
rendering them per harness only works while every harness needs a file. Once
each vendor has its own registry, the rendered files compete with the registry
instead of feeding it. So the box installs no plugins or MCP servers for claude
or pi. Those are the user's dotfiles concern, apart from herdr's own pi
integration (see below).

claude writes its plugin installs into `~/.claude/settings.json`, so that file
must not be a symlink into a version-controlled directory, or the installs
write into the checkout.

## After upgrading an existing box

Removing a harness from the flake does not remove what an earlier rebuild put
on the box, and nothing here deletes it. Once, by hand, as the agent:

- `rm ~/.local/bin/opencode` and `rm -r ~/.opencode`, the opencode install.
- `rm ~/.claude/skills/basedpyright`, the basedpyright plugin link.

Plugins already installed stay in `~/.claude/settings.json` (`enabledPlugins`)
and `~/.pi/agent/settings.json` until removed there. A playwright plugin that is
still installed no longer gets the box's per-project output directory or its
PreToolUse hook: it writes screenshots and PDFs into the checkout and runs a
non-isolated browser profile, unless your dotfiles set
`PLAYWRIGHT_MCP_OUTPUT_DIR` and `PLAYWRIGHT_MCP_ISOLATED` (and a hook of their
own) themselves. The pi browser config
`~/.pi/config/pi-agent-browser-native/config.json` pins a store chromium path in
`browser.executablePath`. Point it at `/run/current-system/sw/bin/chromium`,
which keeps working across rebuilds.

## Lingering

The agent account lingers (`users.users.agent.linger`). That is what makes
"at boot" true for everything above. Without lingering the agent's user
manager starts at the first login and stops at the last logout. The install
service, the launcher path unit, the podman prune timer and the herdr server
would all wait for a session, and `/run/user/1000` would come and go under
anything that kept a socket there. With lingering they run from boot and that
runtime directory persists.

## Recognising a broken launcher

There is no error anywhere. An unwrapped claude works perfectly and carries no
`HERDR_AGENT`. The probe is to ask the two
shells and follow the link:

```sh
zsh -lic 'command -v claude'         # what a human session gets
bash -lc 'command -v claude'         # what an ssh command gets
readlink -f ~/.local/bin/claude      # where the link actually ends
```

The first two must both answer `/home/agent/.local/bin/claude`, and the third
must answer `/etc/yolobox/bin/claude`. When a bare launcher has displaced the wrapper, the two
shells disagree. A check that runs only over a non-interactive, non-login
shell never sources `~/.zshrc`, so it measures a shell nobody uses and misses
the displacement; that is why the probe asks both shells.

## herdr's pi integration

pi's own herdr integration writes `~/.pi/agent/extensions/herdr-agent-state.ts`
(`herdr integration install pi`, run by the `herdr-pi` phase of `yolobox-harness-install`). It is a
vendor-owned file, not one the flake renders. Never add a tmpfiles rule that
deletes a herdr-installed integration file. `systemd-tmpfiles-resetup.service` reapplies `r` rules on
every switch, so the rule would uninstall the integration at the next switch.
The symptom, pi missing from the herd with no error, would look exactly like
an integration that never installed.

A guard on the extension file's existence (`[ -f ~/.pi/agent/extensions/herdr-agent-state.ts ]`)
would leave an outdated integration in place forever: once the file exists,
nothing touches it again, a herdr update that bumps the integration version is
not picked up, and `herdr integration status` reports `pi: outdated` with
nothing reading that output. The guard is therefore `probe_has '^pi: current' herdr integration status ||
herdr integration install pi`, which captures the status output first and greps the
captured text (a `| grep -q` would die of SIGPIPE under `pipefail`, see
[install phases](#install-phases)), so a version bump is
picked up on the next run of the install service, without deleting the
extensions file by hand.
