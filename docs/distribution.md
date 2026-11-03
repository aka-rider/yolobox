# Distribution

`yo` ships as a package, and `yo bootstrap` builds the VM from a published flake ref. No checkout of this repo exists in the guest. This page explains why that shape was chosen and how its failures look. The release mechanics are in [the release workflow notes](../.github/workflows/release.md).

## Flake ref, not a checkout

`yo bootstrap` does not push this repo into the VM, and the VM holds no checkout of it. `yo` is a distributed package, installed with `brew install aka-rider/tap/yolobox` or run with `nix run github:aka-rider/yolobox`. `yo bootstrap` runs `nixos-rebuild boot --impure --flake 'github:aka-rider/yolobox/v<version>#yolobox'`, with no push and no guest checkout.

The version is baked into `yolobox_cli/config.py` when the package is built. The release workflow takes the published release's tag as the single source of truth and writes it into `.version`, inside the tagged tree, so both the brew tarball and a later nix fetch of the tag see the same number (see [Moving the tag](../.github/workflows/release.md#moving-the-tag)). `yo bootstrap --version` overrides the ref for anyone running a fork wholesale, for example `yo bootstrap --version github:you/yolobox/your-branch`. `yo` then remembers that ref on the host for that machine, and its printed rebuild remedies use it (see [yo.md](../yo.md#bootstrap---version)).

The ref is checked before anything runs, and only three spellings pass: a release (`1.7.2`), a branch or tag of `aka-rider/yolobox` (`main`), and `github:OWNER/REPO/REF`. A path, a `path:`, `file:` or `git+file:` ref and a `#` are refused. The box would otherwise build, as root, from a tree the agent can write: a checkout pushed into the VM is agent-owned, so the agent could commit a `wheel` grant between the push and the build. Building a fork means pushing it to GitHub.

The old `git add`-before-Nix discipline now applies only to a local clone someone is hacking on. A file the guest has never seen is simply a file the flake ref does not contain, not an untracked file that Nix skips.

## Each platform ships its own files

macOS runs the Lima driver and Linux runs the libvirt driver, so a host ships only what its driver reads. The brew formula (macOS only) installs `yo`, `aws-broker`, every module of `yolobox_cli/` except `libvirt.py` and `libvirt_host.py`, and `lima/yolobox.yaml`. The nix package, built per system, installs `yo`, `aws-broker` and the whole package, then on Linux removes `lima.py` and adds `libvirt/domain.xml` and `linux/*`, and on macOS removes `libvirt.py` and `libvirt_host.py` and adds `lima/yolobox.yaml`. `cli.driver_for` is the only importer of either driver module, so the missing one is never looked for. `TestEachPlatformShipsItsOwnFiles` fails when a platform lacks a file its driver reads or ships one of the other platform's, and `TestDriversAreImportedOnlyByCli` fails when another module imports a driver.

The version literal is in `yolobox_cli/config.py`, so that is the file both packagers patch.

## Bootstrap reboot decision

`yo bootstrap` decides whether the VM needs a reboot by comparing the two readlinks that [The ESP holds one kernel](../nix/base.md#the-esp-holds-one-kernel) describes, the same pair `yo gc` compares before it runs `nix-collect-garbage -d`. If they are equal, the built generation is already running and bootstrap skips the reboot. If they differ, `nixos-rebuild boot` has just installed a generation the box has not started, so bootstrap restarts the VM and checks again. It aborts with a `df /boot` hint if the two still disagree, which is the half-failed bootloader install that page covers.

The restart itself can hang. Bootstrap stops the VM through the driver (`state.driver().stop()`) and then runs the same `vm_up` as `yo up`, with the passphrase derived beforehand so a missing 1Password key leaves the box running. On Lima the stop is `limactl stop` with a 300 s timeout, and lima's stop hang (see [TODO.md](../TODO.md)) makes it fail at the end of that time; on libvirt it is the watcher described in [yo.md](../yo.md#the-libvirt-driver), with the same 300 s. Bootstrap then aborts, saying the built generation is installed but not booted and that the herdr machine and the 1Password and AWS forwards did not run, and prints the driver's recovery. On Lima:

1. Run `limactl stop yolobox`.
2. If that hangs too, run `yo ssh sudo systemctl reboot` and `kill -9` the leftover hostagent.
3. Re-run `yo bootstrap`. It sees the two generations agree and finishes the steps the first run never reached.

## Failure shapes

Building from a published ref has failure shapes of its own. Each is recognisable on its own.

### The repo goes private

`nixos-rebuild` fetches the flake ref over an anonymous `git+https` or `github:` fetch with no credentials. A private `aka-rider/yolobox` therefore makes every `yo bootstrap` and every customisation rebuild fail with a fetch or auth error. The repo staying public is load-bearing for every box in the field, not a publishing preference.

### `YO_VERSION` is still `dev`

Running `./yo` straight out of an unreleased clone gives no version to pin a ref from. The `flake_ref` function refuses on stderr rather than guess at a ref that may not exist, and its message asks for `--version` (`pass --version with what the VM should build from`). Recognise it by the exact wording of that refusal, not by a bare Nix fetch error.

### A branch that moved, built at its old commit

A ref can name a branch, and a branch moves. nix caches a fetched `github:` ref for `tarball-ttl`, an hour by default, so a rebuild within that hour builds the commit the branch held at the first fetch. That once made `yo bootstrap --version <branch>` exit 0 with "already running the built generation" while the branch had moved on, and nothing in the box said which commit it was built from, because `/etc/yolobox/flake-ref` names only the ref.

Two changes close it. Every rebuild `yo` runs or prints, and `yolobox-guest rebuild`, passes `--refresh` (`NIX_REBUILD_FLAGS` in `yolobox_cli/release.py`; the guest script spells the same flags, and a consistency test keeps them equal), so nix refetches a ref it is given by name, as the printed hint and the guest's rebuild do. And before it builds, `yo bootstrap` resolves the ref in the box with `nix flake metadata --json --refresh <ref>` (`release.locked_rev()`) and prints `building <ref> at <commit>`. A ref the box cannot resolve stops the bootstrap there, with nix's own message, before anything is built.

The commit is not stamped into the system. A stamp in `/etc` was tried: it made every commit a new generation, so a commit that changed only docs cost each box a reboot, and a 1Password approval, at its next bootstrap. CI's eval job checks that a docs-only change keeps the system derivation. The price is that a running box does not say which commit it came from; `yo bootstrap`'s line says it at build time, and the build then names that commit itself (`--flake github:OWNER/REPO/<commit>#yolobox`, `release.nixos_rebuild_argv()`), so a push in between cannot change what is built. `YOLOBOX_FLAKE_REF` stays the ref, so `/etc/yolobox/flake-ref` keeps naming the branch the box follows, and the system is the same derivation whichever spelling fetched the source.

Recognise a stale build: the `building <ref> at <commit>` line names a commit that is not the branch's tip (`ls-remote` against the repository on the host shows the tip).

### A ref older than the host-OS-free flake

Releases up to 1.9.1 passed the host OS into the flake as `YOLOBOX_HOST_OS`, and their `flake.nix` throws when it is not `darwin` or `linux`. Later `yo` no longer passes it ([nix/base.md](../nix/base.md#no-host-os-in-the-guest)), so it cannot build those trees. `yo bootstrap --version 1.9.1` (or any release below 1.9.2) is refused as a usage error that names the release and the command that bootstraps with that release's own `yo` (`nix run github:aka-rider/yolobox/v1.9.1 -- bootstrap`; the brew tap has no versioned formulae), and a rebuild hint for a box remembered at such a release offers that or `yo bootstrap` to this `yo`'s release. An unreleased build of `main` still carries the last release in `.version`; `yo` treats it like `dev` and requires `--version` rather than build that old release. A branch, a non-release tag or a fork's ref has no version to compare, so `yo` cannot tell its age: one cut before this change fails in nix with `YOLOBOX_HOST_OS is ""`, and needs a `yo` of the same age, or a rebase onto a newer tree. The reverse is fine: an older `yo` passes `YOLOBOX_HOST_OS` to a newer flake, which ignores it.

### The box and the Mac's `yo` come from different releases

The guest does not ship `yo`, because `yo` drives a host tool (`limactl` and `~/.lima` on a Mac, `virsh` on a Linux host) that the guest lacks. Instead `nix/base.nix` writes the release into `/etc/yolobox/version`. It also writes `/etc/yolobox/flake-ref` (the exact `github:OWNER/REPO/REF` it was built from) and `/etc/yolobox/hostname`, which `yolobox-guest rebuild switch` reads to rebuild the box from inside it. It writes no host OS: the guest is never told which host runs it (see [nix/base.md](../nix/base.md#no-host-os-in-the-guest)). `yo status` prints both versions and, when they differ, a warning that names `yo bootstrap` as the fix, which makes `yo status` exit 1. A box built before that file existed reports `unknown`.

`cli.main()` calls the driver's `require_tool()` (`limactl` for Lima, `virsh` and its siblings for libvirt) before every command that talks to a box. `--help`, `--version` and `yo help` never reach it, and that holds by construction. `build_parser()` declares `--version` as `action="version"`, and argparse raises `SystemExit` for it and for `--help` inside `parse_args()`, before `main()` reaches `require_tool()`; the `help` subcommand sets `needs_driver=False`. `nix run github:aka-rider/yolobox` inside a guest fails loudly on that preflight for every other command, never with a bare `command not found`.

### `nix flake check` throws with no explanation

`flake.nix` reads `YOLOBOX_USERNAME` and `YOLOBOX_FLAKE_REF` from the environment and `throw`s when either is unset, because Nix has no pure way to learn the host username or the tree the box was built from (see [Two accounts](accounts.md#two-accounts)). A bare `nix flake check` hits that throw at once. It needs `--impure` with `YOLOBOX_USERNAME=$(id -un)` and `YOLOBOX_FLAKE_REF=github:OWNER/REPO/REF` (any valid ref does for a check), like every `nixos-rebuild` here. The guest's system is the evaluating nix's own (`aarch64-linux` or `x86_64-linux`), so `--system aarch64-linux` picks an Apple silicon Mac's guest and `--system x86_64-linux` picks an Intel Mac's or a Linux PC's guest, on any machine. An older `yo` also passes `YOLOBOX_HOST_OS`; the flake ignores it.

A third variable, `YOLOBOX_HOSTNAME`, is optional, unlike the other two. Unset or empty it means `yolobox`, so CI, a bare `nix flake check` and every box built before the variable existed evaluate as they did. A set value must match `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?` (one RFC 1123 label), otherwise `flake.nix` throws with a message that names the variable and the rule. That is the right place for the check because the value reaches the kernel hostname, the DHCP request and tailscale's `--hostname`, and a bad one would fail late and obscurely in any of them. `yo` sets it from `-m NAME` (see [Several machines](../yo.md#several-machines)). The value comes only from the operator's own command line, is checked once by `yo` when it parses `-m` and once by the flake, and is visible only as the guest's hostname, its tailnet node name and the hostname in its DHCP request on the Lima or passt NAT. It is not a secret and it carries no authority. Checking the throw by hand: `YOLOBOX_USERNAME=$(id -un) YOLOBOX_FLAKE_REF=github:aka-rider/yolobox/ci YOLOBOX_HOSTNAME=Bad_Name nix eval --impure .#nixosConfigurations.yolobox.config.networking.hostName`. CI has no step for it, because `tests/test_consistency.py` keeps the flake's pattern equal to the one `yo` enforces, and a flipped pattern fails there.

`YOLOBOX_FLAKE_REF` is required, like `YOLOBOX_USERNAME`, and for a reason of its own. `yo` sets it to the `github:OWNER/REPO/REF` it builds from, and the box keeps that ref in `/etc/yolobox/flake-ref`, which `yolobox-guest rebuild` reads to rebuild from the same tree. Were it optional, a rebuild by hand without it would delete the stamp, and the next rebuild would have to guess `v<version>`, which can be a tag the box's CPU has no build for. So unset, empty or not of the form `github:OWNER/REPO/REF`, the flake throws, and the message names the variable. Checking the throw by hand: `env -u YOLOBOX_FLAKE_REF YOLOBOX_USERNAME=$(id -un) nix eval --impure .#nixosConfigurations.yolobox.config.environment.etc."yolobox/flake-ref".text`. CI checks it too.
