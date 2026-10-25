# Distribution

`yo` ships as a package, and `yo bootstrap` builds the VM from a published flake ref. No checkout of this repo exists in the guest. This page explains why that shape was chosen and how its failures look. The release mechanics are in [the release workflow notes](../.github/workflows/release.md).

## Flake ref, not a checkout

Earlier, `yo bootstrap` pushed this repo into the VM over git (`guest_repo_ensure`, a `yolobox` git remote, `git push`). It then ran `nixos-rebuild --flake '<guest path>#yolobox'` against that pushed-in checkout. Now `yo` is a distributed package, installed with `brew install aka-rider/tap/yolobox` or run with `nix run github:aka-rider/yolobox`. `yo bootstrap` runs `nixos-rebuild boot --impure --flake 'github:aka-rider/yolobox/v<version>#yolobox'`, with no push and no guest checkout.

The version is baked into `yo` when the package is built. The release workflow takes the published release's tag as the single source of truth and writes it into `.version`, inside the tagged tree, so both the brew tarball and a later nix fetch of the tag see the same number (see [Moving the tag](../.github/workflows/release.md#moving-the-tag)). `YOLOBOX_FLAKE` overrides the ref for anyone running a fork wholesale, for example `YOLOBOX_FLAKE=github:you/yolobox/your-branch yo bootstrap`.

The old `git add`-before-Nix discipline now applies only to a local clone someone is hacking on. A file the guest has never seen is simply a file the flake ref does not contain, not an untracked file that Nix skips.

## Bootstrap reboot decision

`yo bootstrap` decides whether the VM needs a reboot by comparing the two readlinks that [The ESP holds one kernel](../nix/base.md#the-esp-holds-one-kernel) describes, the same pair `yo gc` compares before it runs `nix-collect-garbage -d`. If they are equal, the built generation is already running and bootstrap skips the reboot. If they differ, `nixos-rebuild boot` has just installed a generation the box has not started, so bootstrap restarts the VM and checks again. It aborts with a `df /boot` hint if the two still disagree, which is the half-failed bootloader install that page covers.

The restart itself can hang. `limactl restart` stops the VM first, and lima's stop hang (2026-10-17, see [TODO.md](../TODO.md)) makes it fail after three minutes. Bootstrap then aborts, saying the built generation is installed but not booted and that the herdr machine and the 1Password and AWS forwards did not run. To recover:

1. Run `limactl stop yolobox`.
2. If that hangs too, run `yo ssh sudo systemctl reboot` and `kill -9` the leftover hostagent.
3. Re-run `yo bootstrap`. It sees the two generations agree and finishes the steps the first run never reached.

## Failure shapes

This shape introduced failures that the old push-based one never had. Each is recognisable on its own.

### The repo goes private

`nixos-rebuild` fetches the flake ref over an anonymous `git+https` or `github:` fetch with no credentials. A private `aka-rider/yolobox` therefore makes every `yo bootstrap` and every customisation rebuild fail with a fetch or auth error. The repo staying public is load-bearing for every box in the field, not a publishing preference.

### `YO_VERSION` is still `dev`

Running `./yo` straight out of an unreleased clone gives no version to pin a ref from. The `flake_ref` function refuses on stderr rather than guess at a ref that may not exist, and names `YOLOBOX_FLAKE` as the way out. Recognise it by the exact wording of that refusal, not by a bare Nix fetch error.

### The box and the Mac's `yo` come from different releases

The guest no longer ships `yo`. It once did, only so `yo --version` could name the release that built the box, and every other subcommand needs `limactl` and `~/.lima`, which the guest lacks. Instead `nix/base.nix` writes the release into `/etc/yolobox/version`. `yo status` prints both versions and warns on stderr when they differ, naming `yo bootstrap` as the fix. A box built before that file existed reports `unknown`.

`main()` in `yo` still has a `limactl` preflight guard, and `--help` and `--version` never reach it. That holds by construction. `build_parser()` declares `--version` as `action="version"`, and argparse raises `SystemExit` for both flags inside `parse_args()`, before `main()` reaches `require_tool("limactl", ...)`. `nix run github:aka-rider/yolobox` inside a guest fails loudly on that preflight, never with a bare `command not found`.

### `nix flake check` throws with no explanation

`flake.nix` reads `YOLOBOX_USERNAME` from the environment and `throw`s when it is unset, because Nix has no pure way to learn the host username (see [Two accounts](accounts.md#two-accounts)). A bare `nix flake check` hits that throw at once. It needs `--impure` with `YOLOBOX_USERNAME=$(id -un)`, like every `nixos-rebuild` here.

### A tag that does not evaluate

Published tags up to and including v1.0.1 cannot be rebuilt. `nix/base.nix` carried a stray `programs.git-lfs.enable = true`, introduced by `e5934aa` ("enable git-lfs globally"), two lines below the correct `programs.git.lfs.enable`. `programs.git-lfs` is not a NixOS option in any nixpkgs branch, so `nixos-rebuild --flake 'github:aka-rider/yolobox/v1.0.1#yolobox'` dies in evaluation with "The option `programs.git-lfs' does not exist". `212f5e9` removed the line, so v1.1.0 and later evaluate. v1.0.0 and v1.0.2 were not checked, and any tree between `e5934aa` and `212f5e9` carries the line.

The tags were not moved or yanked, because moving a published tag republishes different content under a name someone may have fetched. A box on one of them, or built from a ref in that range, must use v1.1.0 or newer. The `eval` job in `verify.yml` now catches this class before a release.
