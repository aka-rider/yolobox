# herdr: the VM runs its own server

`herd-server.nix` runs herdr's server inside the VM as the `agent` account.
It is a `systemd.user` service wanted by `default.target` and gated by
`ConditionUser = agent`. It is up from boot because the agent account
lingers (see [Lingering](harnesses.md#lingering)). Panes opened against that
server are real ptys, so herdr detects an agent inside them natively, the
same way it detects one on the Mac.

## Machine panes

The Mac used to run the only herdr server. `yo enter` forwarded that
server's unix socket into the guest over `ssh -R`, to
`/run/yolobox/herd-host.<pane>.sock`, and set `YOLOBOX_HERD`,
`HERDR_PANE_ID` and `HERDR_SOCKET_PATH` so guest-side hooks could report
through it. That existed for one reason: a host pane's foreground process was
`ssh`, not the agent, so herdr had nothing of its own to detect.

Agents now run in herdr's own machine panes, not in `yo enter`. `yo enter`
stays a plain landing shell (`ssh -t ... exec $SHELL -l`). A session started
there is invisible to herdr, and so is one from `yo ssh`, `yo code`, `yo zed`
or a t3-spawned agent. None of them opens a pty on the guest herdr server, so
all of them show as `unknown` or not at all. The place to run an agent is a
workspace opened on the saved `yolobox` machine. `cmd_enter` says so on
stderr when `HERDR_ENV=1`, meaning the command itself runs inside a herdr
pane. Inside the guest, navigation is left to the agent's own shell setup. Nothing named `yo` exists in the guest.

### The regression this design fixes

`a91644c` ("herdr v0.9 supports cross-machine connections natively") removed
`yo enter`'s forwarding of the Mac pane's herdr socket, on the theory that
native machine panes made it unnecessary. Nothing yet moved agents into those
panes, so the removal shipped with no replacement. It left this shape:

- `herdr machine list` on the Mac was empty, because nothing had run `herdr
  machine add` to adopt the VM's server.
- Inside the VM, `herdr pane list` was empty even with `pi` plainly running.
  Its foreground process sat directly under `sshd`, `yo enter`'s own session,
  never under a pty the guest server had opened, and it carried none of
  `HERDR_ENV`, `HERDR_PANE_ID` or `HERDR_SOCKET_PATH`, the env a real herdr
  pane gets for free.
- From the Mac an agent running that way looked permanently idle, with no
  error anywhere.

### Adoption, not startup

The Mac attaches to the guest server as a saved SSH machine, adopted rather
than started. `herdr machine add <alias> --label yolobox` checks the running
server's capabilities and never its process ancestry, so the systemd unit and
`machine add` never fight over who owns the process. `yo bootstrap` does this
through `ensure_herdr_machine()`, which runs at its end. It checks `herdr
machine list --json` for an entry whose SSH target is `yolobox`. If there is
none, it runs `herdr machine add yolobox --label yolobox` and raises on a
non-zero exit with herdr's own stderr.

Adoption is also why `herdrPkg` has to stay in `environment.systemPackages`
(`harnesses.nix`). `machine add` installs its own binary into
`~/.local/bin/herdr` only when no compatible binary turns up on its probe
list, and that list includes `/run/current-system/sw/bin/herdr`. Drop the
package from `systemPackages` and a fresh `machine add` tries to install its
own binary instead of adopting the system one. A non-interactive install is
refused outright rather than done silently.

## Capability gating

Compatibility is gated by capability, not by version equality. The guest
server reports `endpoint_protocol_generation`, `surface_interest`,
`health_check` and `detached_server_daemon`, and herdr's
`remote_server_restart_reason` gates a saved machine on those four.
`endpoint_protocol_generation` has to equal the client's own constant
exactly. That is coarser than the version string, so the VM's herdr and the
Mac's herdr may run different versions as long as the generation agrees.

The VM's herdr is pinned regardless, through the
`yolobox.harness.herdr.version` and `.hash` valve in `flake.nix`, which holds
the current pin and the reason for it. The pinned nixpkgs lags the Mac's
herdr, and the Mac's herdr, not nixpkgs, sets the generation the guest must
speak. The pin fetches the published `herdr-linux-aarch64` release asset
directly (`pkgs/herdr-bin.nix`). That binary has no `PT_INTERP` and no
`PT_DYNAMIC`, so it is statically linked and runs on NixOS unpatched, with
none of the nix-ld setup a dynamically linked upstream binary would need.

### A saved machine sits stuck

A saved machine that never connects means `remote_server_restart_reason`
refused one of the four capabilities, almost always
`endpoint_protocol_generation`. A Mac herdr and a VM herdr that both look
like "0.9.x" in `herdr status` can still disagree on generation, so read the
generation, not the version string beside it. The remedy is the pin's own
escape hatch: bump `yolobox.harness.herdr.version` and `.hash` to the
generation the Mac's herdr now speaks.

## HERDR_AGENT

`herdr agent list` comes back empty for a session that is plainly running
claude, and no error appears anywhere. herdr classifies a pane's agent by the
name of its foreground process. The box's claude launcher `exec`s
`~/.local/share/claude/versions/<version>` directly, so herdr sees a process
named after a version directory (`2.1.268`, say), never `claude`, and
classification never reaches herdr's screen manifest.

The manifest is fine throughout, which shows the gap is in classification
and not detection. `herdr agent explain --file <pane dump> --agent claude`
matches rule `live_prompt_box` and returns `idle` while `herdr agent list`
shows nothing. The fix is one line in the launcher ahead of its `exec`:
`export HERDR_AGENT=claude`. herdr reads the hint from the process
environment instead of the process name.

herdr's docs warn that `HERDR_AGENT` "cannot be seen if you set it only
inside a VM". That describes a host-side herdr watching into a VM from
outside. It does not apply here, because the server reading the hint runs
inside the VM with the process.

With the hint in place, `herdr agent get` showed all three states live:
`idle` (rule `live_prompt_box`), `working` during a real turn, and `blocked`
(rule `bash_permission_prompt`, steady while a real permission dialog was
up). Plan mode intercepts a destructive command before the permission UI
appears, so re-testing `blocked` needs the pane in manual mode first.

## Host keys for the yolobox alias

`herdr machine add yolobox --label yolobox` fails on strict host-key checking unless the alias pins the guest's real key. The cause, the fix and how to recognise it are in [herdr's alias](../docs/ssh.md#herdrs-alias).
