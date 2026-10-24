# Display, browsers and clipboard

The code is `nix/display.nix`. This file holds the incidents and the reasons the comments there do not carry.

## Display

The display is `:0`, 1920x1080, Xvfb with openbox on it, and `DISPLAY=:0` is set box-wide. Xvfb and openbox are the agent's user units (`systemd.user.services`, `ConditionUser=agent`, up from boot through the agent's lingering, see [Lingering](harnesses.md#lingering)). They are not system units that run as the agent, because a system unit logs to the system journal and reading that takes the `systemd-journal` group. The reason that group is not worth having is in [No journal group](../docs/accounts.md#no-journal-group). Read these units' logs with `journalctl --user -u openbox` as the agent.

Xvfb listens on the unix socket only (`-nolisten tcp`) and has no `-auth`, so any local uid, the operator and root included, can connect. That is no boundary, because the display is the agent's own and holds nothing the agent does not already hold.

## Browsers

Nothing in this repo wraps a browser. Two vendor tools reach the display instead. The first is claude's official `playwright` plugin (`npx @playwright/mcp@latest`), which runs headed because `DISPLAY` is set. The second is `pi-agent-browser-native` over Vercel's `agent-browser` CLI, which is headless unless told `--headed`. Both are installed by `yolobox-harness-install` (see [Plugins, not MCP files](harnesses.md#plugins-not-mcp-files)). The old guard that refused loudly when the X socket was missing went away with the per-engine wrappers that carried it, so a missing socket now means upstream's silent fall back to headless.

The box supplies one thing, the browser binary, because Playwright's own browser downloads do not run on NixOS. `PLAYWRIGHT_MCP_EXECUTABLE_PATH` points at nixpkgs' `chromium` (155, cached for aarch64). agent-browser finds the same chromium on `PATH` and through the extension's own config file. `PLAYWRIGHT_MCP_CAPS=vision,pdf,devtools` turns on the three capabilities that are off by default.

Three settings are deliberate and easy to break:

- `AGENT_BROWSER_EXECUTABLE_PATH` stays unset, because pi-agent-browser-native disables its managed session restore whenever that variable is present.
- `agent-browser install` must never be run. It downloads a glibc Chrome for Testing that cannot execute here, and leaves a binary that looks installed and is not.
- `PLAYWRIGHT_MCP_ISOLATED=1` is set box-wide, and isolated mode and a user-data-dir are mutually exclusive: the server throws when both are set. The choice is to run every launch isolated with no persistent profile, so no cookie or login outlives the launch that created it. `PLAYWRIGHT_MCP_USER_DATA_DIR` must never be set beside it.

## Output directory and the PreToolUse hook

The claude launcher (see [The launcher](harnesses.md#the-launcher)) computes a per-project output directory, not a profile. Before `exec` it derives the project from the logical cwd: the nearest ancestor of `$PWD`, as spelled, whose `pwd -P` equals git's `--show-toplevel`. That is the same walk as `logical_toplevel()` in `yo`, for the same reason (see [The mirror](../yo.md#the-mirror)). `~/wrk` is a symlink to `~/Developer`, and counting the `../` that `--show-cdup` prints up the logical path, which the launcher once did, climbs to the wrong directory when a symlink sits below the root.

The launcher takes the project path relative to `$HOME`, turns slashes into `--`, creates `$HOME/artifacts/<project>` and exports it as `PLAYWRIGHT_MCP_OUTPUT_DIR`. A cwd at or outside `$HOME` leaves the variable at its box default. Snapshots and unnamed screenshots land there because Playwright honours that variable directly.

A screenshot with an explicit `filename` does not. Playwright resolves an explicit name against the MCP workspace root, the first root the client advertised or else the server's own cwd (upstream design, microsoft/playwright#42487 and #42494), and Claude Code has advertised its own launch directory as MCP root #1 since 2.1.203. So a named screenshot would land inside whatever checkout the session started in, which is what keeping output under `~/artifacts` exists to prevent. The `PreToolUse` hook `yolobox-playwright-artifacts` in `nix/display.nix` rewrites `filename` before the tool runs. The comments there carry the rewrite rules, the `..` check that runs before the "already under the output dir" exemption, and the fixed `unnamed` fallback. The hook reaches claude only through the launcher's `--settings` flag, because the `/etc` managed-settings tier is discarded whole (see [The launcher](harnesses.md#the-launcher)).

## Clipboard

Copying inside pi in a herdr pane used to die with ``Clipboard unavailable: install `xclip` or `xsel`, or check X11 access``. The string is pi's, not herdr's (`packages/coding-agent/src/utils/clipboard.ts` in earendil-works/pi). pi emitted OSC 52 on its own only when `SSH_CONNECTION`, `SSH_CLIENT` or `MOSH_CONNECTION` was set, or when there was no `DISPLAY` at all. That was deliberate (pi#9618): OSC 52 cannot be confirmed to have landed, so pi does not trust it when a display exists to try first. This box sets `DISPLAY=:0` everywhere, and herdr panes are children of the systemd herdr server, so they carry no `SSH_*` variable either. With no wayland, no xclip and no OSC 52, pi threw.

A real `xclip` would have been worse than the error, because it would succeed into Xvfb's clipboard, which nobody sees. That is the same reasoning that keeps far2l at `withTTYX = false` (see [far2l](pkgs/far2l.md#build)). So from 2026-10 until 2026-10-19 `nix/display.nix` shipped `xclip` and `xsel` as one shim that wrote OSC 52 to the pane's tty. pi v1.2.0 (commit 3e477eab, 2026-10-12, closing pi#10114, #10826 and #10835) made the shim unnecessary with an opt-in, `PI_CLIPBOARD_OSC52=1`. It is documented as sending the copied text to the terminal with OSC 52 "after the local clipboard write ... such as a multiplexer attached from another machine". The maintainers chose an opt-in over auto-detecting `HERDR_ENV` or `TMUX`, so the box sets it itself: `nix/lib/agent-env.nix` exports it to every agent process, and the shim was deleted on 2026-10-19.

The mechanism, read from pi v1.3.0:

1. `isRemoteSession()` is true when `PI_CLIPBOARD_OSC52 === "1"`.
2. pi still tries `xclip` and `xsel` first, gets ENOENT and ignores it.
3. It then writes a raw `ESC]52;c;<base64>BEL` to stdout, the pane's pty, and returns success. It never reaches the "install xclip" throw.
4. Every pi copy path (`/copy`, fullscreen selection, the copy message, the auth URL) goes through `copyToClipboard`.
5. herdr's pane parser turns the sequence into `ServerMessage::Clipboard` and sends it to the foreground client, and the Mac client runs `pbcopy`.

The size limit is pi's: 100,000 base64 characters, about 75 KB of text, above which pi throws "exceeds the OSC 52 size limit". herdr's own cap is 192 KiB and is never reached first. Claude Code (2.1.303) never needed any of this. It emits raw OSC 52 itself whether or not `xclip` exists, and skips it only when a native tool succeeded on a VTE terminal.

Two limits are herdr's, and the opt-in cannot lift them. The Mac herdr client drops a Clipboard message from a machine that is not the active one, so a copy made in a background machine's pane is lost. And herdr's own selection and copy mode in machine panes copies server-side in 0.9.x (herdr#4833, open). That copy happens in the herdr server, which has no tty, so pi's opt-in does not reach it. `TODO.md` carries both.

To recognise a regression, look for the exact pi error string coming back. It means `PI_CLIPBOARD_OSC52` is missing from that process's environment (`tr '\0' '\n' < /proc/<pi pid>/environ | grep PI_CLIPBOARD`), or pi is older than 1.2.0 (`pi --version`). The other shape is a copy that "succeeds" but never reaches the Mac. There pi did its part and herdr dropped the message, for instance because the pane's machine was not the active one.

## Learned the hard way

- `openbox` waits for the X socket in `ExecStartPre`. A `Restart=` loop trips systemd's start limit and stays failed.
- Use `${pkgs.xvfb}/bin/Xvfb`. `lib.getExe` on it names a binary that does not exist.
- No tmpfiles rule for `/tmp/.X11-unix`. systemd ships one.
- The screen-record pidfile lives in `~/.local/state/yolobox`, because `/run/user/$UID` dies at logout while the backgrounded `ffmpeg` survives. `SIGINT` finalizes the mp4 and `SIGKILL` corrupts it.
- Headed screenshots without `fonts.packages` render as tofu.
- Default ffmpeg has no x11grab, so `nix/display.nix` uses `ffmpeg-full`.
- Two sessions share one browser. Chromium's singleton lock forwards the second launch (about 8 s). That is not isolation and not a failure.
- Chromium's storage flushes lazily, so a session killed without `browser_close` can lose its last write.
