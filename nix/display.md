# Display, browsers and clipboard

The code is `nix/display.nix`. This file holds the incidents and the reasons the comments there do not carry.

## Display

The display is `:0`, 1920x1080, Xvfb with openbox on it, and `DISPLAY=:0` is set for the agent only, in `nix/lib/agent-env.nix`. Xvfb and openbox are the agent's user units (`systemd.user.services`, `ConditionUser=agent`, up from boot through the agent's lingering, see [Lingering](harnesses.md#lingering)). They are not system units that run as the agent, because a system unit logs to the system journal and reading that takes the `systemd-journal` group. The reason that group is not worth having is in [No journal group](../docs/accounts.md#no-journal-group). Read these units' logs with `journalctl --user -u openbox` as the agent.

Xvfb listens on the unix socket only (`-nolisten tcp`) and has no `-auth`, so any local uid, the operator and root included, can connect. That is no boundary, because the display is the agent's own and holds nothing the agent does not already hold.

## Browsers

Nothing in this repo wraps a browser, and the box installs no browser plugin. The box supplies the browser binary, because Playwright's own browser downloads do not run on NixOS: `PLAYWRIGHT_MCP_EXECUTABLE_PATH` points at nixpkgs' `chromium`, which is also on `PATH`. Browser tools the user installs reach the display through `DISPLAY=:0`, and a headed run needs the X socket. Nothing guards a missing X socket, so a missing socket can mean a silent fall back to headless.

`yolobox-screen-record` writes its recordings to `~/artifacts/`. Nothing else in the box routes browser output.

## Clipboard

Symptom: copying inside pi in a herdr pane dies with ``Clipboard unavailable: install `xclip` or `xsel`, or check X11 access``. The string is pi's, not herdr's (`packages/coding-agent/src/utils/clipboard.ts` in earendil-works/pi). pi emitted OSC 52 on its own only when `SSH_CONNECTION`, `SSH_CLIENT` or `MOSH_CONNECTION` was set, or when there was no `DISPLAY` at all. That was deliberate (pi#9618): OSC 52 cannot be confirmed to have landed, so pi does not trust it when a display exists to try first. The box sets `DISPLAY=:0` for the agent, and herdr panes are children of the systemd herdr server, so they carry no `SSH_*` variable either. With no wayland, no xclip and no OSC 52, pi threw.

A real `xclip` would be worse than the error, because it would succeed into Xvfb's clipboard, which nobody sees. So the box installs neither `xclip` nor `xsel`. pi v1.2.0 (commit 3e477eab, closing pi#10114, #10826 and #10835) added an opt-in, `PI_CLIPBOARD_OSC52=1`, documented as sending the copied text to the terminal with OSC 52 "after the local clipboard write ... such as a multiplexer attached from another machine". The maintainers chose an opt-in over auto-detecting `HERDR_ENV` or `TMUX`, so the box sets it itself: `nix/lib/agent-env.nix` exports it to every agent process.

The mechanism, read from pi v1.3.0:

1. `isRemoteSession()` is true when `PI_CLIPBOARD_OSC52 === "1"`.
2. pi still tries `xclip` and `xsel` first, gets ENOENT and ignores it.
3. It then writes a raw `ESC]52;c;<base64>BEL` to stdout, the pane's pty, and returns success. It never reaches the "install xclip" throw.
4. Every pi copy path (`/copy`, fullscreen selection, the copy message, the auth URL) goes through `copyToClipboard`.
5. herdr's pane parser turns the sequence into `ServerMessage::Clipboard` and sends it to the foreground client, and the host client runs its clipboard command (`pbcopy` on macOS).

The size limit is pi's: 100,000 base64 characters, about 75 KB of text, above which pi throws "exceeds the OSC 52 size limit". herdr's own cap is 192 KiB and is never reached first. Claude Code (2.1.303) never needed any of this. It emits raw OSC 52 itself whether or not `xclip` exists, and skips it only when a native tool succeeded on a VTE terminal.

Two limits are herdr's, and the opt-in cannot lift them. The host's herdr client drops a Clipboard message from a machine that is not the active one, so a copy made in a background machine's pane is lost. And herdr's own selection and copy mode in machine panes copies server-side in 0.9.x. That copy happens in the herdr server, which has no tty, so pi's opt-in does not reach it.

To recognise a regression, look for the exact pi error string coming back. It means `PI_CLIPBOARD_OSC52` is missing from that process's environment (`tr '\0' '\n' < /proc/<pi pid>/environ | grep PI_CLIPBOARD`), or pi is older than 1.2.0 (`pi --version`). The other shape is a copy that "succeeds" but never reaches the host. There pi did its part and herdr dropped the message, for instance because the pane's machine was not the active one.

## Learned the hard way

- `openbox` waits for the X socket in `ExecStartPre`. A `Restart=` loop trips systemd's start limit and stays failed.
- Use `${pkgs.xvfb}/bin/Xvfb`. `lib.getExe` on it names a binary that does not exist.
- No tmpfiles rule for `/tmp/.X11-unix`. systemd ships one.
- The screen-record pidfile lives in `~/.local/state/yolobox`, because `/run/user/$UID` dies at logout while the backgrounded `ffmpeg` survives. `SIGINT` finalizes the mp4 and `SIGKILL` corrupts it. The file survives a reboot too, and the kernel reuses its pid, so `start` and `stop` trust it only when `/proc/<pid>/comm` is `ffmpeg` and otherwise remove it. Without that check, `stop` sent `SIGINT` to whatever agent process had the pid, and `start` refused with "already recording". Recognise it by `stop` saying "stale pid file" after a reboot.
- Headed screenshots without `fonts.packages` render as tofu.
- Default ffmpeg has no x11grab, so `nix/display.nix` uses `ffmpeg-full`.
- Two sessions share one browser. Chromium's singleton lock forwards the second launch, which is slow. That is not isolation and not a failure.
- Chromium's storage flushes lazily, so a session killed without `browser_close` can lose its last write.
