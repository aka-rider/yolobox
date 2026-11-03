"""The flake ref a box is built from, version drift and the nix environment of a rebuild."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shlex
from typing import List, Optional

from . import config, proc, sshx, state, ui
from .ui import YoError


UPSTREAM_FLAKE = "github:aka-rider/yolobox"
# Every pattern here is matched with fullmatch, so none carries ^ or $.
RELEASE_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
PARTIAL_VERSION_RE = re.compile(r"v?\d+(\.\d+)?")
GIT_REF_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
GIT_REF_RE = re.compile(GIT_REF_PATTERN)
# Exactly owner/repo/ref on github, nothing else: a path:, file: or git+file:
# ref, an absolute path or a '#' would let the box build from a tree the agent
# controls, or smuggle a second flake attribute into the rebuild.
GITHUB_FLAKE_RE = re.compile(
    r"github:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/%s" % GIT_REF_PATTERN
)


def version_drift(host: str, guest: Optional[str]) -> Optional[str]:
    if guest is None or host == "dev" or host == guest:
        return None
    return "yo: this host's yo is %s but the box was built by %s — rebuild it: %s" % (
        host,
        guest,
        state.yo_cmd("bootstrap"),
    )


def flake_ref(version: Optional[str]) -> str:
    """The flake ref `bootstrap --version VERSION` builds the box from."""
    if version is None:
        if config.YO_VERSION == "dev":
            raise YoError(
                "this is an unreleased build with no pinned flake — pass --version "
                "with what the VM should build from (e.g. %s, "
                "or a flake ref such as github:you/yolobox/branch)"
                % state.yo_cmd("bootstrap", "--version", "my-branch")
            )
        return "%s/v%s" % (UPSTREAM_FLAKE, config.YO_VERSION)
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
    """argparse `type=` of `bootstrap --version`: a bad ref is a usage error."""
    try:
        return flake_ref(value)
    except YoError as failure:
        raise argparse.ArgumentTypeError(failure.message)


def flake_ref_file() -> str:
    return os.path.join(state.state_dir(), "%s.flake-ref" % state.box.name)


def remembered_flake_ref() -> Optional[str]:
    """The ref the last bootstrap built this machine from, if it ever did."""
    text = proc.read_text_file(flake_ref_file(), errors="surrogateescape")
    ref = text.strip() if text is not None else ""
    return ref or None


def rebuild_hint() -> str:
    ref = remembered_flake_ref()
    if ref is not None:
        try:
            ref = flake_ref(ref)
        except YoError:
            ui.err(
                "yo: %s holds %r, which is no longer an accepted flake ref — not using "
                "it; run %s to rewrite it"
                % (
                    flake_ref_file(),
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
    return "%s sudo %s nixos-rebuild switch --impure --flake %s" % (
        state.yo_cmd("ssh"),
        " ".join(nix_env("$(id -un)")),
        shlex.quote(ref + "#yolobox"),
    )


def built_by() -> Optional[str]:
    """The yolobox version the box was built by; None for a box that predates the file."""
    return (
        sshx.probe(sshx.OPERATOR, ["cat", "/etc/yolobox/version"]).stdout.strip()
        or None
    )


def helper_skew(what: str) -> str:
    drift = version_drift(config.YO_VERSION, built_by())
    if drift:
        return "%s.\n    %s" % (what, drift[len("yo: ") :])
    return "%s.\n    rebuild it: %s" % (what, rebuild_hint())


def host_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


# The variables every nix command on the box needs: flake.nix throws without the
# first two, and falls back to "yolobox" without the third, which would rename an
# -m machine back at its next rebuild. `username` is a value or a shell
# expression, as the caller needs.
NIX_ENV_USERNAME = "YOLOBOX_USERNAME"
NIX_ENV_HOST_OS = "YOLOBOX_HOST_OS"
NIX_ENV_HOSTNAME = "YOLOBOX_HOSTNAME"


def nix_env(username: str) -> List[str]:
    return [
        "%s=%s" % (NIX_ENV_USERNAME, username),
        "%s=%s" % (NIX_ENV_HOST_OS, state.driver().host_os),
        "%s=%s" % (NIX_ENV_HOSTNAME, state.box.hostname),
    ]
