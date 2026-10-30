# Tailscale pin

The code is `nix/pkgs/tailscale.nix`, an `overrideAttrs` of nixpkgs' `tailscale` that moves it to a newer release. `nix/tailscale.nix` hands it to `services.tailscale.package`. It exists because `nixos-unstable` is a channel: it advances only after Hydra has built a nixpkgs commit, so a release that merged into `master` is days from the channel. The box wants the newest tailscale, since it sits on a tailnet and a stale release keeps known fixes out.

## The pin removes itself

The file asserts that nixpkgs' own version is older than the pin. The first `nix flake update` that brings a nixpkgs with the pinned version or newer fails the build with `nixpkgs ships tailscale X, not older than the pin Y`. The fix is to delete `nix/pkgs/tailscale.nix`, the `package` line in `nix/tailscale.nix`, this file and its row in `docs/README.md`. Without the assertion the override would silently hold the box back on the pinned release after nixpkgs had moved past it, because the override wins over whatever the channel ships.

## Bumping it

Set `version`, then both hashes.

- `hash` is the NAR hash of the source: `nix flake prefetch github:tailscale/tailscale/v<version> --json` prints it as `hash`.
- `vendorHash` is the hash of the Go module set. It cannot be read off the release. Build once with the old value, or take the new one from the same version's `pkgs/by-name/ta/tailscale/package.nix` in nixpkgs `master`, and let nix verify it: a wrong value fails the build with the `got:` hash.

Nothing else in nixpkgs' expression is overridden, so a release that changes the build itself (a new Go toolchain, new `subPackages`) needs the nixpkgs expression read first. The check is `nix build` on the toplevel, in the guest.

## History

- 1.104.4 (2026-10-28), pinned while `nixos-unstable` carried 1.104.2 (nixpkgs PR 579890 merged to `master` on 2026-10-28, the day after the channel's 2026-10-27 commit). 1.104.2 to 1.104.4 touches `ssh/tailssh` hardening (the box runs `--ssh=false`), a DERP response-body bound, a magicsock disco-key check, and serve refusing to proxy to the tailscaled socket. Nothing in it changes Linux routing, fwmarks, rule priorities or netfilter handling, which is what `nix/egress.nix` and the tunnel modules depend on.
