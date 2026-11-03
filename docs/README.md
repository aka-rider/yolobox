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
| [ssh.md](ssh.md) | One `ControlPath` per ssh role, the owned block in `ssh.config`, the 1Password forward and how `yo` heals reverse forwards, `ForwardAgent no`, the host-key pin, picking a git identity in the guest, and why the Mac never reads guest-influenced paths. |
| [aws.md](aws.md) | The host-side AWS credential broker: the allowlist, the guest config, the reverse-socket transport, the broker's lifetime and health states, minting, the bounds on one request, and `aws-creds`. |
| [disk.md](disk.md) | Recognising a full disk and the incidents that taught the recovery order. |
| [disk-encryption.md](disk-encryption.md) | LUKS on the root partition: the 1Password-derived passphrase, the secrets that still live on disk, the initrd sshd lima can reach, the password-agent delivery, the in-place migration, growing, and how to recognise an unlock failure. |
| [host-network.md](host-network.md) | VPN shapes on the host that blackhole guest traffic, the DNS probe of lima's gateway, the gateway-blocked marker for a box that blocks the gateway on purpose, the DNS sinkhole and UDP flows stuck after a VPN disconnects. |
| [linux-host.md](linux-host.md) | The Linux host: why `qemu:///system` with passt and not Lima, the session connection or a bridge, the Lima address mirror, the host table, the host port window and its cap, the operator key in 1Password with `AllowUsers` and the anti-spoof rule, fatal config gaps, the IPv6 difference, the tailnet residual, polkit, stopping the domain and its registration-window residual, the passt socket directory and its ACL, the disk-unlock listener check, sizing a box at creation, and how to recognise each failure. |
| [distribution.md](distribution.md) | Building the box from a flake ref instead of a checkout, the refs `--version` accepts, what each platform ships, the bootstrap reboot decision, and the failure shapes that follow. |
| [../yo.md](../yo.md) | The host-side CLI: the path mirror, `yo cp`, the guest helper, the package layout and patch convention, which commands heal the forwards, the `network:` line and the `yo status` exit code, the single parser, the output filter and the `YO_VERSION` literal. |
| [../lima/yolobox.md](../lima/yolobox.md) | `lima/yolobox.yaml` as read-once config, checking a running instance, why guest UDP is never mirrored, which guest ports reach the host, pinned settings, and the forwarder bug. |
| [../nix/base.md](../nix/base.md) | The ESP size, a sysctl key the kernel lacks, same-uid ptrace, memory pressure and the swap size option, growing the root disk, NAT and IP forwarding, the wheel-only sudo wrapper, and why the guest is never told its host OS (each host difference gated on the fact it needs). |
| [../nix/lsp.md](../nix/lsp.md) | Why the box has no direnv, `lspWrapper` and its walk up to the project's `devbox.json`, how Zed is wired, and why claude-code and pi are outside it. |
| [../nix/harnesses.md](../nix/harnesses.md) | The `claude` launcher, the version keeper, install phases, pi, lingering, herdr's pi integration, and upgrading an existing box. |
| [../nix/herd-server.md](../nix/herd-server.md) | The VM's own herdr server, machine panes, capability gating, `HERDR_AGENT`, and host keys for the `yolobox` alias. |
| [../nix/display.md](../nix/display.md) | The virtual display, browsers, the clipboard, and lessons learned. |
| [../nix/podman.md](../nix/podman.md) | Container output and podman's events kept out of the journal: the journal flood, the log driver, the events logger, the journald cap, how to recognise a flood, and what an old container keeps. |
| [../linux/host-setup.md](../linux/host-setup.md) | The Linux host's one-time setup script: the libvirt repository and version gate, no libvirt network, the polkit rule, the `yolobox-vm` account, the host egress table and why it has no `ExecStop`, the loopback-resolver check and the pool. |
| [../nix/egress.md](../nix/egress.md) | The egress table: what the guest may not reach on the host and the LAN and why, the gateway NAT to the host's loopback, DNS as the one exception, `lanAllow`, the tailnet as an ACL-controlled entrance, how it composes with another nft table, and the three tiers (full, recovery, last resort) with the rollback and the scripts that load them. |
| [../nix/hardening.md](../nix/hardening.md) | The journal of the box's attack surface: blocked kernel modules, the setuid wrapper allowlist, sysctls and kernel parameters, what was kept and why, what the article does that we skip, the inbound firewall picture, and how to run the acceptance test. |
| [../nix/tailscale.md](../nix/tailscale.md) | The optional tailnet: why `tailscale0` is trusted, what that opens and why its ACL needs a review, sshd on two interfaces, `tailscaled-set`, DNS and logging, relayed-contact log lines, login, box-local routes. |
| [../nix/guest/yolobox-guest.md](../nix/guest/yolobox-guest.md) | The guest helper script: `net-check` and the gateway-blocked marker, `rebuild`, the one way to rebuild the box from inside it, the project walk, `BUILD_DIRS` and `LOCAL_CACHES`, guards, version skew, and the gc tiers. |
| [../nix/pkgs/tailscale.md](../nix/pkgs/tailscale.md) | Why tailscale is pinned ahead of nixpkgs, the assertion that removes the pin, and how to bump it. |
| [../.github/workflows/release.md](../.github/workflows/release.md) | Moving the release tag, why releases stay mutable, `bump-tap` and `actions/checkout`, the early refusals, `verify.yml`, and pinned actions. |

## No revision pointers

A story states its facts in place and does not point into history. History gets rewritten and tags get deleted, so a hash or a tag tells the reader nothing once the history it names is gone. Say in words what a commit did and what a release showed. Links between story files are fine, because they name a heading. Hashes of other repos and pinned action SHAs are exempt.

## No operator specifics

The repo is generic, and one operator's setup is not a default or a fact about every user. A story or a comment that names one person's machine, network, vaults, providers or projects is wrong for everyone else. A box's own setup lives in its `/etc/yolobox/local.nix`, which `nix/base.nix` imports when it exists.

An incident story keeps the failure mode, the evidence shape and how to recognise it. It drops dates, disk and memory sizes from one box, project and machine names, providers, and whose decision it was. Write "a host" and "a box". Public upstream facts, such as the issue numbers of lima, herdr and tailscale, stay.
