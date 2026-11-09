"""The flake ref a box is built from, version drift and the nix environment of a rebuild."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
from typing import List, NamedTuple, Optional

from . import config, proc, sshx, state
from .ui import YoError


UPSTREAM_FLAKE = "github:aka-rider/yolobox"
# Every pattern here is matched with fullmatch, so none carries ^ or $. The
# ones with \d are re.ASCII: str patterns otherwise take any Unicode digit.
RELEASE_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)", re.ASCII)
PARTIAL_VERSION_RE = re.compile(r"v?\d+(\.\d+)?", re.ASCII)
GIT_REF_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
GIT_REF_RE = re.compile(GIT_REF_PATTERN)
# Exactly owner/repo/ref on github, nothing else: a path:, file: or git+file:
# ref, an absolute path or a '#' would let the box build from a tree the agent
# controls, or smuggle a second flake attribute into the rebuild.
GITHUB_FLAKE_RE = re.compile(
    r"github:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/%s" % GIT_REF_PATTERN
)
# What YO_VERSION and YO_SOURCE_REV hold in a yo no build stamped: a checkout's.
UNRELEASED = "dev"


def version_drift(host: str, guest: str) -> Optional[str]:
    """Why the box should be bootstrapped again, or None: it was built by another
    version than this yo. An unreleased yo (`dev`) has no version to compare."""
    if host != UNRELEASED and guest != host:
        return "yo: the box was built by %s and this yo is %s — run %s" % (
            guest,
            host,
            state.yo_cmd("bootstrap"),
        )
    return None


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
    return named_flake_ref(version)


def own_release_ref() -> Optional[str]:
    """The ref this yo builds by default: the commit it was built from when nix
    stamped one (YO_SOURCE_REV), else its release tag. None for an unreleased
    build (`dev`), which has none it can build.

    The commit comes first because a `nix run github:aka-rider/yolobox` yo is
    built from main while its .version names the last release: building that
    tag would give a box of another tree than the yo, and `yo status` would
    report a skew no bootstrap can fix."""
    if config.YO_SOURCE_REV != UNRELEASED:
        return "%s/%s" % (UPSTREAM_FLAKE, config.YO_SOURCE_REV)
    if config.YO_VERSION == UNRELEASED:
        return None
    return "%s/v%s" % (UPSTREAM_FLAKE, config.YO_VERSION)


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
    one a usage error."""
    try:
        return named_flake_ref(value)
    except YoError as failure:
        raise argparse.ArgumentTypeError(failure.message)


# The flags of every nixos-rebuild yo runs, and yolobox-guest's `rebuild`
# passes the same. --impure lets the flake read the nix_env variables.
NIX_REBUILD_FLAGS = ("--impure",)


# The lock the box's nixos-rebuild holds while it runs, and the exit code flock -n
# gives when another holds it; yolobox-guest's `rebuild` takes the same lock. An
# interrupted yo bootstrap leaves its nixos-rebuild running in the box, and a
# second must not start beside it.
REBUILD_LOCK = config.FACTS["rebuild_lock"]
REBUILD_BUSY_EXIT = config.FACTS["rebuild_busy_exit"]


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


def nixos_rebuild_argv(action: str, username: str, pinned: str) -> List[str]:
    """The box's `sudo ... flock ... nixos-rebuild ACTION` of `pinned`, a
    pinned_ref: the commit itself is built, so what yo printed is what is
    built even if the branch moves meanwhile, nix refuses a tree whose NAR hash
    is not the one metadata saw, and the box stamps that exact tree as the one
    `yolobox-guest rebuild` builds again."""
    return [
        "sudo",
        *nix_env(username, pinned),
        "flock",
        "-n",
        "-E",
        str(REBUILD_BUSY_EXIT),
        REBUILD_LOCK,
        "nixos-rebuild",
        action,
        *NIX_REBUILD_FLAGS,
        "--flake",
        "%s#yolobox" % pinned,
    ]


def pinned_ref(ref: str, source: "LockedSource") -> str:
    """github:OWNER/REPO/<rev>?narHash=<hash> for `ref` (github:OWNER/REPO/REF)
    at `source`."""
    return "%s/%s?narHash=%s" % (ref.rsplit("/", 1)[0], source.rev, source.nar_hash)


# A commit, and the SRI NAR hash of its tree (sha256 in base64: 43 characters
# and one `=`).
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
NARHASH_RE = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")


class LockedSource(NamedTuple):
    """What a ref resolved to: the commit and the NAR hash of its tree."""

    rev: str
    nar_hash: str


def locked_rev(ref: str) -> LockedSource:
    """What `ref` names now, fetched afresh by the box's own nix.

    A branch moves, so the rebuild that follows builds this commit by name
    (nixos_rebuild_argv), not the branch, and the NAR hash makes nix refuse a
    tree that differs from the one this metadata saw."""
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
        locked = json.loads(completed.stdout)["locked"]
        rev, nar_hash = locked["rev"], locked["narHash"]
    except (ValueError, KeyError, TypeError):
        raise YoError(
            "nix flake metadata printed no locked commit and narHash for %s%s"
            % (ref, proc.what_it_said(completed))
        )
    if not isinstance(rev, str) or not COMMIT_RE.fullmatch(rev):
        raise YoError("nix flake metadata locked %s at %r, not a commit" % (ref, rev))
    if not isinstance(nar_hash, str) or not NARHASH_RE.fullmatch(nar_hash):
        raise YoError(
            "nix flake metadata locked %s with narHash %r, not an SRI sha256"
            % (ref, nar_hash)
        )
    return LockedSource(rev, nar_hash)


def host_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


# The variables every nix command on the box needs: flake.nix throws without the
# first and the third (the third is stamped into /etc/yolobox/flake-ref), and
# falls back to "yolobox" without the second, which would rename an -m machine
# back at its next rebuild. `username` is a value or a shell expression, as the
# caller needs; `ref` is the flake ref to build, a pinned_ref for a rebuild.
# yolobox-guest's `rebuild` sets the same three, from the stamp and the kernel.
NIX_ENV_USERNAME = config.FACTS["nix_env_username"]
NIX_ENV_HOSTNAME = config.FACTS["nix_env_hostname"]
NIX_ENV_FLAKE_REF = config.FACTS["nix_env_flake_ref"]


def nix_env(username: str, ref: str) -> List[str]:
    return [
        "%s=%s" % (NIX_ENV_USERNAME, username),
        "%s=%s" % (NIX_ENV_HOSTNAME, state.box.hostname),
        "%s=%s" % (NIX_ENV_FLAKE_REF, ref),
    ]
