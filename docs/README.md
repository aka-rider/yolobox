# Where the stories live

`CONSTITUTION.md` lists the rules for changing this repo. `CLAUDE.md` is a short tour. The reasoning behind a rule, the incident that earned it and the way to recognise its failure are written down here, next to the code they explain.

## The convention

- A story belongs to one owning file. It lives in `<stem>.md` beside that file, so `nix/tailscale.md` sits beside `nix/tailscale.nix` and `yo.md` sits beside `yo`.
- A short "why" stays in the code comment. The doc keeps what a comment cannot hold: the incident, the evidence, and how to recognise the failure when it comes back.
- A story that spans several owning files goes in `docs/<topic>.md`.
- Links name a heading or a code symbol, never a line number. Line numbers rot; headings and symbols can be searched.
- Before editing a file, check whether a `<stem>.md` sits beside it, and read it first.

Sibling `.md` files are safe to add. No package installs them: `nix/pkgs/yolobox.nix` and `nix/guest.nix` copy files by explicit path.

## The files

| File | What it owns |
|---|---|
| [accounts.md](accounts.md) | The operator and agent accounts, why the agent is a new account and not a stripped one, locked passwords, `AuthorizedKeysCommand`, the missing journal group, and what the split does not protect. |
| [ssh.md](ssh.md) | One `ControlPath` per ssh role, the owned block in `ssh.config`, the 1Password forward and how `yo` heals reverse forwards, `ForwardAgent no`, the host-key pin, and the two GitHub accounts. |
| [aws.md](aws.md) | The Mac-side AWS credential broker: the allowlist, the guest config, the reverse-socket transport, the broker's lifetime, minting, and `aws-creds`. |
| [disk.md](disk.md) | Recognising a full disk and the incidents that taught the recovery order. |
| [disk-encryption.md](disk-encryption.md) | LUKS on the root partition: the 1Password-derived passphrase, the initrd sshd lima can reach, the password-agent delivery, the in-place migration, growing, and how to recognise an unlock failure. |
| [mac-network.md](mac-network.md) | Mac VPN shapes that blackhole guest traffic, what the optional kill switch changes about them, the DNS sinkhole, UDP flows stuck after a VPN disconnects, and lima listening on ports 80 and 443. |
| [distribution.md](distribution.md) | Building the box from a flake ref instead of a checkout, the bootstrap reboot decision, and the failure shapes that follow. |
| [../yo.md](../yo.md) | The Mac-side CLI: the path mirror, `yo link`, `yo cp`, the single parser, the output filter, the `YO_VERSION` literal, and opening a guest URL. |
| [../lima/yolobox.md](../lima/yolobox.md) | `lima/yolobox.yaml` as read-once config, checking a running instance, why guest UDP is never mirrored, pinned settings, and the forwarder bug. |
| [../nix/base.md](../nix/base.md) | The ESP size, same-uid ptrace, memory pressure, and growing the root disk. |
| [../nix/harnesses.md](../nix/harnesses.md) | The `claude` launcher, the version keeper, install phases, pi, the plugins, lingering, and herdr's pi integration. |
| [../nix/herd-server.md](../nix/herd-server.md) | The VM's own herdr server, machine panes, capability gating, `HERDR_AGENT`, and host keys for the `yolobox` alias. |
| [../nix/display.md](../nix/display.md) | The virtual display, browsers, the Playwright output hook, the clipboard, and lessons learned. |
| [../nix/t3.md](../nix/t3.md) | The vendor-installed t3code unit and its drop-in, service status, version pruning, pairing, and log noise. |
| [../nix/killswitch.md](../nix/killswitch.md) | The optional kill switch: opting in through `/etc/yolobox/local.nix`, its nft table and why it has no stop hook, how it fails closed, DNS over the tailnet, the exit node, and `/etc/yolobox/network.json`. |
| [../nix/tailscale.md](../nix/tailscale.md) | The tailnet: firewall, sshd on two interfaces, `tailscaled-set`, DNS and logging, relayed-contact log lines, login, the exit node, serve readiness, box-local routes. |
| [../nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md) | The guest helper script: the project walk, `BUILD_DIRS` and `LOCAL_CACHES`, guards, version skew, and the gc tiers. |
| [../nix/pkgs/far2l.md](../nix/pkgs/far2l.md) | The far2l build and S3 through the broker. |
| [../.github/workflows/release.md](../.github/workflows/release.md) | Moving the release tag, `bump-tap` and `actions/checkout`, the early refusals, `verify.yml`, and pinned actions. |
