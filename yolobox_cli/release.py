"""The flake ref a box is built from, version drift and the nix environment of a rebuild."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
from typing import List, Optional, Tuple

from . import config, proc, sshx, state, ui
from .ui import YoError


UPSTREAM_FLAKE = "github:aka-rider/yolobox"
# Every pattern here is matched with fullmatch, so none carries ^ or $. The
# ones with \d are re.ASCII: str patterns otherwise take any Unicode digit.
RELEASE_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)", re.ASCII)
VERSION_TRIPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)", re.ASCII)
PARTIAL_VERSION_RE = re.compile(r"v?\d+(\.\d+)?", re.ASCII)
GIT_REF_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
GIT_REF_RE = re.compile(GIT_REF_PATTERN)
# Exactly owner/repo/ref on github, nothing else: a path:, file: or git+file:
# ref, an absolute path or a '#' would let the box build from a tree the agent
# controls, or smuggle a second flake attribute into the rebuild.
GITHUB_FLAKE_RE = re.compile(
    r"github:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/%s" % GIT_REF_PATTERN
)
# GitHub owner and repo names are case-insensitive, so the upstream release
# check must be too.
UPSTREAM_RELEASE_RE = re.compile(
    re.escape(UPSTREAM_FLAKE) + r"/v(\d+)\.(\d+)\.(\d+)", re.IGNORECASE | re.ASCII
)

# The first release whose flake never asks for the host OS. An older release's
# flake throws unless YOLOBOX_HOST_OS is darwin or linux, and this yo no longer
# passes it, so it cannot build one (nix/base.md#no-host-os-in-the-guest).
FIRST_HOST_OS_FREE_RELEASE = (1, 9, 2)


def release_triple(version: str) -> Optional[Tuple[int, int, int]]:
    """The (X, Y, Z) of a plain X.Y.Z version; None for `dev` or anything else."""
    found = VERSION_TRIPLE_RE.fullmatch(version)
    if found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def version_drift(
    host: str, guest: Optional[str], built_from: Optional[str]
) -> Optional[str]:
    """What to do about a box built by another release than this yo, or None.

    A box built by a newer release is not rebuilt by this yo: `yo bootstrap`
    would build the older release over it. Versions that are not X.Y.Z cannot be
    ordered, so a box that differs from this yo is advised the rebuild.
    `built_from` is the ref the box was bootstrapped from with --version
    (state.remembered_flake_ref()), or None: the rebuild advised then names
    that ref, because a plain `yo bootstrap` would move the box to this yo's
    release and forget the ref.
    """
    if guest is None or host == "dev" or host == guest:
        return None
    host_triple = release_triple(host)
    guest_triple = release_triple(guest)
    if (
        host_triple is not None
        and guest_triple is not None
        and guest_triple > host_triple
    ):
        return (
            "yo: the box was built by %s, newer than this host's yo %s — upgrade yo "
            "(%s would build %s over it)"
            % (guest, host, state.yo_cmd("bootstrap"), host)
        )
    if built_from is None:
        return (
            "yo: this host's yo is %s but the box was built by %s — rebuild it: %s"
            % (host, guest, state.yo_cmd("bootstrap"))
        )
    return (
        "yo: this host's yo is %s but the box was built by %s from %s — rebuild it "
        "from that ref: %s"
        % (
            host,
            guest,
            built_from,
            state.yo_cmd("bootstrap", "--version", shlex.quote(built_from)),
        )
    )


def flake_ref(version: Optional[str]) -> str:
    """The flake ref `bootstrap --version VERSION` builds the box from."""
    if version is None:
        own = own_release_ref()
        if own is None:
            raise YoError(
                "this yo (%s) is an unreleased build with no release it can build — "
                "pass --version with what the VM should build from (e.g. %s, "
                "or a flake ref such as github:you/yolobox/branch)"
                % (
                    config.YO_VERSION,
                    state.yo_cmd("bootstrap", "--version", "my-branch"),
                )
            )
        return own
    ref = named_flake_ref(version)
    old = old_release(ref)
    if old is not None:
        raise YoError(old_release_refusal(old))
    return ref


def old_release_refusal(old: str) -> str:
    return (
        "cannot build release %s, %s — build it with that release's own yo "
        "instead, %s"
        % (
            old,
            OLD_RELEASE_WHY,
            old_release_yo(old),
        )
    )


def own_release_ref() -> Optional[str]:
    """This yo's own release ref; None for an unreleased build, which has none
    it can build: `dev`, or a build of main whose .version still names a
    release that predates FIRST_HOST_OS_FREE_RELEASE. A build of main stamped
    with a later release cannot be told from that release and claims it
    (docs/distribution.md)."""
    if config.YO_VERSION == "dev":
        return None
    ref = "%s/v%s" % (UPSTREAM_FLAKE, config.YO_VERSION)
    return None if old_release(ref) is not None else ref


# Why old_release() refs are refused, worded to follow "release X".
OLD_RELEASE_WHY = (
    "whose flake needs the host OS, which this yo no longer passes "
    "(every release before %s does)"
    % ".".join(str(part) for part in FIRST_HOST_OS_FREE_RELEASE)
)


def old_release(ref: str) -> Optional[str]:
    """The X.Y.Z of `ref` when it is an upstream release that predates
    FIRST_HOST_OS_FREE_RELEASE; None for any other ref. A branch or a fork's
    ref carries no version, so it cannot be checked here (docs/distribution.md)."""
    release = UPSTREAM_RELEASE_RE.fullmatch(ref)
    if release is None:
        return None
    parts = tuple(int(part) for part in release.groups())
    if parts >= FIRST_HOST_OS_FREE_RELEASE:
        return None
    return ".".join(str(part) for part in parts)


def old_release_yo(version: str) -> str:
    """The command that bootstraps this machine with release `version`'s own yo."""
    return "nix run %s/v%s -- %s" % (
        UPSTREAM_FLAKE,
        version,
        state.yo_cmd("bootstrap")[len("yo ") :],
    )


def named_flake_ref(version: str) -> str:
    """The flake ref a --version value names, before any release check."""
    release = RELEASE_VERSION_RE.fullmatch(version)
    if release:
        return "%s/v%s" % (UPSTREAM_FLAKE, release.group(1))
    if PARTIAL_VERSION_RE.fullmatch(version):
        raise YoError("a release is X.Y.Z, got %r" % version)
    if version == "latest":
        raise YoError(
            "there is no latest ref; omit --version for this yo's own release, "
            "or name a branch"
        )
    if GIT_REF_RE.fullmatch(version):
        return "%s/%s" % (UPSTREAM_FLAKE, version)
    if GITHUB_FLAKE_RE.fullmatch(version):
        return version
    raise YoError(
        "--version must be a release (1.7.2), a branch or tag on aka-rider/yolobox "
        "(main), or github:OWNER/REPO/REF, got %r" % version
    )


def bootstrap_version(value: str) -> str:
    """argparse `type=` of `bootstrap --version`: the ref the value names, a bad
    one a usage error. It reads no state: the old-release check names the
    machine in its remedy, and main() chooses the machine only after parsing
    (old_release_problem)."""
    try:
        return named_flake_ref(value)
    except YoError as failure:
        raise argparse.ArgumentTypeError(failure.message)


def old_release_problem(args: argparse.Namespace) -> Optional[str]:
    """The usage error of a `bootstrap --version` that names a release this yo
    cannot build (old_release), once the machine is chosen; None otherwise."""
    if args.version is None:
        return None
    old = old_release(args.version)
    return None if old is None else old_release_refusal(old)


def rebuild_hint() -> str:
    ref = state.remembered_flake_ref()
    old = old_release(ref) if ref is not None else None
    if old is not None:
        # A command naming that ref would fail in the old flake, and one naming
        # this yo's release would silently move the box: offer both instead.
        if own_release_ref() is not None:
            move = "run %s to move the box to this yo's release" % state.yo_cmd(
                "bootstrap"
            )
        else:
            move = "run %s to move the box to a ref this yo can build" % (
                state.yo_cmd("bootstrap", "--version", "<ref>")
            )
        return "%s, or %s to keep it on release %s, %s" % (
            move,
            old_release_yo(old),
            old,
            OLD_RELEASE_WHY,
        )
    if ref is not None:
        try:
            ref = flake_ref(ref)
        except YoError:
            ui.err(
                "yo: %s holds %r, which is no longer an accepted flake ref — not using "
                "it; run %s to rewrite it"
                % (
                    state.flake_ref_file(),
                    ref,
                    state.yo_cmd("bootstrap", "--version", "<ref>"),
                )
            )
            ref = None
    if ref is None:
        try:
            ref = flake_ref(None)
        except YoError as failure:
            ui.err("yo: %s" % failure.message)
            return "run %s with a flake ref the box should build from, then retry" % (
                state.yo_cmd("bootstrap", "--version", "<ref>")
            )
    return "%s sudo %s nixos-rebuild switch %s --flake %s" % (
        state.yo_cmd("ssh"),
        " ".join(nix_env("$(id -un)", ref)),
        " ".join(NIX_REBUILD_FLAGS),
        shlex.quote(ref + "#yolobox"),
    )


# The flags of every nixos-rebuild yo runs or prints, and yolobox-guest's
# `rebuild` passes the same (tests/test_consistency.py). --impure lets the
# flake read the nix_env variables. --refresh refetches the ref: a branch
# moves, and nix would otherwise reuse a fetch younger than tarball-ttl (an
# hour) and silently build the commit it held then. yo bootstrap's own build
# names a commit (nixos_rebuild_argv), so there the flag is harmless; it
# matters for locked_rev's lookup and for the printed hint and yolobox-guest's
# rebuild, which build the ref by name.
NIX_REBUILD_FLAGS = ("--impure", "--refresh")


# The lock the box's nixos-rebuild holds while it runs, and the exit code flock -n
# gives when another holds it. yolobox-guest's `rebuild` spells both the same
# (tests/test_consistency.py). An interrupted yo bootstrap leaves its
# nixos-rebuild running in the box, and a second must not start beside it.
REBUILD_LOCK = "/run/yolobox-rebuild.lock"
REBUILD_BUSY_EXIT = 75


def rebuild_busy_message() -> str:
    return (
        "another nixos-rebuild is already running in %s (it holds %s), perhaps one "
        "an interrupted bootstrap left behind. Wait for it to finish, see it with %s, "
        "then run this again"
        % (
            state.box.name,
            REBUILD_LOCK,
            state.yo_cmd("ssh", "pgrep", "-a", "nixos-rebuild"),
        )
    )


def nixos_rebuild_argv(action: str, username: str, ref: str, rev: str) -> List[str]:
    """The box's `sudo ... flock ... nixos-rebuild ACTION` of `ref` at commit `rev`.

    It builds the commit itself (github:OWNER/REPO/<rev>), so what yo printed
    is what is built, even if the branch moves meanwhile. YOLOBOX_FLAKE_REF
    stays `ref`, so the box stamps the branch it follows, and the system is the
    same derivation either way: it depends on the source, not on its name."""
    return [
        "sudo",
        *nix_env(username, ref),
        "flock",
        "-n",
        "-E",
        str(REBUILD_BUSY_EXIT),
        REBUILD_LOCK,
        "nixos-rebuild",
        action,
        *NIX_REBUILD_FLAGS,
        "--flake",
        "%s#yolobox" % pinned_ref(ref, rev),
    ]


def pinned_ref(ref: str, rev: str) -> str:
    """github:OWNER/REPO/<rev> for `ref` (github:OWNER/REPO/REF), at commit `rev`."""
    return "%s/%s" % (ref.rsplit("/", 1)[0], rev)


VERSION_STAMP = "/etc/yolobox/version"
# What cat says of a path that does not exist; any other failure is not "absent".
NO_SUCH_FILE = "No such file or directory"
# How much of a stamp that is not a version is quoted back.
STAMP_SHOWN_CHARS = 60


def built_by() -> Optional[str]:
    """The yolobox version the box was built by; None for a box that predates the
    stamp. A stamp that reads neither X.Y.Z nor dev is shown escaped, so the
    drift advice can quote it without taking it for a version."""
    completed = sshx.probe(sshx.OPERATOR, ["cat", VERSION_STAMP])
    if completed.returncode != 0:
        if NO_SUCH_FILE in completed.stderr:
            return None
        raise sshx.box_failure("cat %s" % VERSION_STAMP, completed)
    version = completed.stdout.strip()
    if not version:
        return None
    if version == "dev" or VERSION_TRIPLE_RE.fullmatch(version):
        return str(version)
    return ui.shown(version[:STAMP_SHOWN_CHARS])


COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def locked_rev(ref: str) -> str:
    """The commit `ref` names now, fetched afresh by the box's own nix.

    A branch moves, so the rebuild that follows builds this commit by name
    (nixos_rebuild_argv), not the branch. Not stamped into the system: a
    stamp would make every commit a new generation, and with it a reboot,
    even for one that changes only docs."""
    completed = sshx.probe(
        sshx.OPERATOR,
        ["nix", "flake", "metadata", "--json", "--refresh", ref],
    )
    if completed.returncode != 0:
        raise YoError(
            "the box could not resolve %s (nix flake metadata, exit %d)%s"
            % (ref, completed.returncode, proc.what_it_said(completed))
        )
    try:
        rev = json.loads(completed.stdout)["locked"]["rev"]
    except (ValueError, KeyError, TypeError):
        raise YoError(
            "nix flake metadata printed no locked commit for %s%s"
            % (ref, proc.what_it_said(completed))
        )
    if not isinstance(rev, str) or not COMMIT_RE.fullmatch(rev):
        raise YoError("nix flake metadata locked %s at %r, not a commit" % (ref, rev))
    return rev


def helper_skew(what: str) -> str:
    drift = version_drift(config.YO_VERSION, built_by(), state.remembered_flake_ref())
    if drift:
        return "%s.\n    %s" % (what, drift[len("yo: ") :])
    return "%s.\n    rebuild it: %s" % (what, rebuild_hint())


def host_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


# The variables every nix command on the box needs: flake.nix throws without the
# first and the third (the third is stamped into /etc/yolobox/flake-ref), and
# falls back to "yolobox" without the second, which would rename an -m machine
# back at its next rebuild. `username` is a value or a shell expression, as the
# caller needs; `ref` is a full github:OWNER/REPO/REF, as flake_ref returns.
# yolobox-guest's `rebuild` sets the same three, from the box's own stamps.
NIX_ENV_USERNAME = "YOLOBOX_USERNAME"
NIX_ENV_HOSTNAME = "YOLOBOX_HOSTNAME"
NIX_ENV_FLAKE_REF = "YOLOBOX_FLAKE_REF"


def nix_env(username: str, ref: str) -> List[str]:
    return [
        "%s=%s" % (NIX_ENV_USERNAME, username),
        "%s=%s" % (NIX_ENV_HOSTNAME, state.box.hostname),
        "%s=%s" % (NIX_ENV_FLAKE_REF, ref),
    ]
