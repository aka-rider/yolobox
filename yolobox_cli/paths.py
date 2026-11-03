"""Mirroring the host's working directory onto the guest's home."""

from __future__ import annotations

import os
from typing import Optional, Sequence

from . import guest, state, ui
from .config import AGENT_HOME
from .sshx import AGENT
from .ui import YoError


def guest_path(path: str, roots: Sequence[str] = (AGENT_HOME,)) -> str:
    if "\0" in path or "\n" in path:
        raise YoError("refusing a guest path with a newline or NUL byte")
    normalised = os.path.normpath(path)
    if not any(
        normalised == root or normalised.startswith(root + "/") for root in roots
    ):
        if tuple(roots) == (AGENT_HOME,):
            raise YoError(
                'refusing to target "%s": no yo command may leave the guest home "%s"'
                % (path, AGENT_HOME)
            )
        raise YoError(
            'refusing to target "%s": outside the guest paths this command may use (%s)'
            % (path, " and ".join('"%s"' % root for root in roots))
        )
    return normalised


def logical_cwd() -> str:
    pwd_env = os.environ.get("PWD", "")
    if os.path.isabs(pwd_env):
        try:
            if os.path.samefile(pwd_env, os.getcwd()):
                return pwd_env
        except OSError:
            pass
    return os.getcwd()


def mirror(mac_path: str) -> Optional[str]:
    host = state.home().rstrip("/")
    if mac_path == host:
        return AGENT_HOME
    if mac_path.startswith(host + "/"):
        return AGENT_HOME + "/" + mac_path[len(host) + 1 :]
    return None


MISSING_PATH_NOTE = 'path "%s" does not exist in the yolobox you are in "%s"'


def dir_for_cwd() -> str:
    desired = mirror(logical_cwd())
    if desired is None:
        ui.err(MISSING_PATH_NOTE % (logical_cwd(), AGENT_HOME))
        return AGENT_HOME
    reported = guest.guest_helper_check(
        guest.guest_helper(AGENT, "landing-dir", [desired]), "landing-dir"
    ).stdout.strip("\n")
    landing = guest_path(reported)
    if landing != desired:
        ui.err(MISSING_PATH_NOTE % (desired, landing))
    return landing


def logical_toplevel(physical_top: str, cwd: str) -> str:
    # git reports the repo root with symlinks resolved, but the mirror rule
    # spells paths as the shell does (say ~/code is a link to ~/src). The
    # root is the nearest ancestor of the logical cwd that resolves to it;
    # cwd's ".." count cannot be used, since a symlink below the root can
    # make the physical and logical depths differ.
    real_top = os.path.realpath(physical_top)
    candidate = cwd
    while os.path.realpath(candidate) != real_top:
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return real_top
        candidate = parent
    return candidate
