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
                % (ui.shown(path), AGENT_HOME)
            )
        raise YoError(
            'refusing to target "%s": outside the guest paths this command may use (%s)'
            % (ui.shown(path), " and ".join('"%s"' % root for root in roots))
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
    # Both spellings of $HOME: when $HOME goes through a symlink (macOS's /var),
    # a cwd the shell did not report (no usable $PWD) comes back from getcwd()
    # resolved. Only the prefix is matched; the rest of the path stays as spelled.
    # $PWD is the shell's word and may hold `..`, so the mirror is normalised
    # lexically (as a shell resolves a logical path, never through symlinks)
    # and placed only if it stays under the guest home.
    home = state.home()
    for host in dict.fromkeys((home.rstrip("/"), os.path.realpath(home).rstrip("/"))):
        if mac_path == host:
            return AGENT_HOME
        if mac_path.startswith(host + "/"):
            mirrored = os.path.normpath(AGENT_HOME + "/" + mac_path[len(host) + 1 :])
            if mirrored == AGENT_HOME or mirrored.startswith(AGENT_HOME + "/"):
                return mirrored
            return None
    return None


MISSING_PATH_NOTE = 'path "%s" does not exist in the yolobox you are in "%s"'


def dir_for_cwd() -> str:
    desired = mirror(logical_cwd())
    if desired is None:
        ui.err(MISSING_PATH_NOTE % (ui.shown(logical_cwd()), AGENT_HOME))
        return AGENT_HOME
    # Checked before it is sent: the helper is asked only for a path under the
    # guest home, with no newline a forged answer could hide behind.
    desired = guest_path(desired)
    reported = guest.guest_helper_check(
        guest.guest_helper(AGENT, "landing-dir", [desired]), "landing-dir"
    ).stdout.strip("\n")
    landing = guest_path(reported)
    if landing != desired:
        ui.err(MISSING_PATH_NOTE % (ui.shown(desired), ui.shown(landing)))
    return landing
