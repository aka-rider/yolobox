# Distribution

`yo` ships as a package, and `yo bootstrap` builds the VM from a published flake ref. No checkout of this repo exists in the guest. This page explains why that shape was chosen and how its failures look. The release mechanics are in [the release workflow notes](../.github/workflows/release.md).

## Flake ref, not a checkout

`yo bootstrap` does not push this repo into the VM, and the VM holds no checkout of it. `yo` is a distributed package, installed with `brew install aka-rider/tap/yolobox` or run with `nix run github:aka-rider/yolobox`. `yo bootstrap` runs `nixos-rebuild boot --impure --flake 'github:aka-rider/yolobox/v<version>#yolobox'`, with no push and no guest checkout.

The version is baked into `yo` when the package is built. The release workflow takes the published release's tag as the single source of truth and writes it into `.version`, inside the tagged tree, so both the brew tarball and a later nix fetch of the tag see the same number (see [Moving the tag](../.github/workflows/release.md#moving-the-tag)). `yo bootstrap --version` overrides the ref for anyone running a fork wholesale, for example `yo bootstrap --version github:you/yolobox/your-branch`. `yo` then remembers that ref on the host for that machine, and its printed rebuild remedies use it (see [yo.md](../yo.md#bootstrap---version)).

The old `git add`-before-Nix discipline now applies only to a local clone someone is hacking on. A file the guest has never seen is simply a file the flake ref does not contain, not an untracked file that Nix skips.

## Bootstrap reboot decision

`yo bootstrap` decides whether the VM needs a reboot by comparing the two readlinks that [The ESP holds one kernel](../nix/base.md#the-esp-holds-one-kernel) describes, the same pair `yo gc` compares before it runs `nix-collect-garbage -d`. If they are equal, the built generation is already running and bootstrap skips the reboot. If they differ, `nixos-rebuild boot` has just installed a generation the box has not started, so bootstrap restarts the VM and checks again. It aborts with a `df /boot` hint if the two still disagree, which is the half-failed bootloader install that page covers.

The restart itself can hang. `limactl restart` stops the VM first, and lima's stop hang (see [TODO.md](../TODO.md)) makes it fail after three minutes. Bootstrap then aborts, saying the built generation is installed but not booted and that the herdr machine and the 1Password and AWS forwards did not run. To recover:

1. Run `limactl stop yolobox`.
2. If that hangs too, run `yo ssh sudo systemctl reboot` and `kill -9` the leftover hostagent.
3. Re-run `yo bootstrap`. It sees the two generations agree and finishes the steps the first run never reached.

## Failure shapes

Building from a published ref has failure shapes of its own. Each is recognisable on its own.

### The repo goes private

`nixos-rebuild` fetches the flake ref over an anonymous `git+https` or `github:` fetch with no credentials. A private `aka-rider/yolobox` therefore makes every `yo bootstrap` and every customisation rebuild fail with a fetch or auth error. The repo staying public is load-bearing for every box in the field, not a publishing preference.

### `YO_VERSION` is still `dev`

Running `./yo` straight out of an unreleased clone gives no version to pin a ref from. The `flake_ref` function refuses on stderr rather than guess at a ref that may not exist, and its message asks for `--version` (`pass --version with what the VM should build from`). Recognise it by the exact wording of that refusal, not by a bare Nix fetch error.

### The box and the Mac's `yo` come from different releases

The guest does not ship `yo`, because every `yo` subcommand but `--version` needs `limactl` and `~/.lima`, which the guest lacks. Instead `nix/base.nix` writes the release into `/etc/yolobox/version`. `yo status` prints both versions and warns on stderr when they differ, naming `yo bootstrap` as the fix. A box built before that file existed reports `unknown`.

`main()` in `yo` still has a `limactl` preflight guard, and `--help` and `--version` never reach it. That holds by construction. `build_parser()` declares `--version` as `action="version"`, and argparse raises `SystemExit` for both flags inside `parse_args()`, before `main()` reaches `require_tool("limactl", ...)`. `nix run github:aka-rider/yolobox` inside a guest fails loudly on that preflight, never with a bare `command not found`.

### `nix flake check` throws with no explanation

`flake.nix` reads `YOLOBOX_USERNAME` and `YOLOBOX_HOST_OS` from the environment and `throw`s when either is unset, because Nix has no pure way to learn the host username or the host OS (see [Two accounts](accounts.md#two-accounts)). A bare `nix flake check` hits that throw at once. It needs `--impure` with `YOLOBOX_USERNAME=$(id -un)` and `YOLOBOX_HOST_OS=darwin` or `linux`, like every `nixos-rebuild` here. `YOLOBOX_HOST_OS` is separate from the CPU: the guest's system is the evaluating nix's own (`aarch64-linux` or `x86_64-linux`), so `--system aarch64-linux` picks the Mac box's guest on any machine.
