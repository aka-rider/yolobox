"""Calling yolobox-guest, and the guest's host keys and root usage."""

from __future__ import annotations

import subprocess
from typing import List, NamedTuple, Optional, Sequence

from . import proc, sshconfig, sshx, state, ui
from .config import AGENT_HOME, FILE_MODE_PRIVATE, HELPER
from .sshx import AGENT, OPERATOR, SshRole
from .ui import YoError


HOST_KEY_GLOB = "/etc/ssh/ssh_host_*_key.pub"


def ensure_guest_known_hosts() -> None:
    # The herdr alias's UserKnownHostsFile has to be pre-populated with the
    # guest's real key: herdr passes -o StrictHostKeyChecking=yes on the
    # command line for every saved machine, which overrides anything weaker
    # this alias sets, so "no known key yet" can never pass. The fetch is
    # first contact only while nothing is pinned; with a pin it is pinned
    # itself (sshx.host_key_fetch_role), so a changed key is refused, never
    # learned anew, and the file is rewritten only after a fetch that passed.
    completed = sshx.ssh_run(
        sshx.host_key_fetch_role(),
        ["sh", "-c", "cat %s" % HOST_KEY_GLOB],
        check=False,
    )
    if completed.returncode != 0:
        raise YoError(
            "could not fetch the guest's ssh host keys (%s) to pin them for the %r ssh "
            "alias%s"
            % (HOST_KEY_GLOB, state.box.herdr_alias, proc.what_it_said(completed))
        )

    lines = sshconfig.known_hosts_lines(
        state.driver().ssh_facts().port, completed.stdout
    )
    if not lines:
        raise YoError(
            "the guest reported no parseable ssh host keys at %s" % HOST_KEY_GLOB
        )

    proc.atomic_write(
        state.known_hosts_path(), "\n".join(lines) + "\n", FILE_MODE_PRIVATE
    )


def guest_helper(
    role: SshRole,
    sub: str,
    args: Sequence[str] = (),
    *,
    stream: bool = False,
    **kwargs,
) -> subprocess.CompletedProcess:
    """`yolobox-guest SUB ARGS` as `role`; the exit code is the caller's to read."""
    if stream:
        kwargs["capture"] = False
        kwargs["filtered"] = True
    return sshx.probe(role, [HELPER, sub, *args], **kwargs)


def agent_paths(sub: str, args: Sequence[str] = (), *, check: bool = True) -> List[str]:
    completed = guest_helper(AGENT, sub, args, binary=True)
    if completed.returncode != 0:
        if check:
            proc.checked(completed, "%s %s" % (HELPER, sub), "on the box")
        else:
            if completed.stderr:
                ui.err(ui.quoted_block(completed.stderr))
            ui.err(
                "yo: the project walk could not read every directory under %s (find exit %d)."
                % (AGENT_HOME, completed.returncode)
            )
            ui.err(
                "    The list below is what was readable; a project under an unreadable "
                "directory is missing from it."
            )
    decoded = completed.stdout.decode("utf-8", "replace")
    return [entry for entry in decoded.split("\0") if entry]


class RootUsage(NamedTuple):
    """The guest's / as df reports it."""

    human_line: str  # `df -h /`'s data line as the guest printed it, for the operator
    total_kib: int  # from `df -kP /`
    used_kib: int


# One ssh round trip for both views: -h for the operator to read, -kP for the numbers.
ROOT_USAGE_PROBE = ["sh", "-c", "df -h / && df -kP /"]
ROOT_USAGE_LINES = 4  # a header and a data line, twice


def df_kib(fields: Sequence[str], column: int) -> Optional[int]:
    if len(fields) > column and proc.is_ascii_number(fields[column]):
        return int(fields[column])
    return None


def guest_root_usage() -> RootUsage:
    """The one place that asks the guest how full / is.

    Raises YoError naming what ssh or df said when the answer is unusable, so a
    caller that must have the numbers fails with the detail and one that can do
    without catches it and shows it.
    """
    completed = sshx.ssh_run(OPERATOR, ROOT_USAGE_PROBE)
    if completed.returncode != 0:
        raise YoError(
            "cannot read the VM's root usage from df (exit %d)%s"
            % (completed.returncode, proc.what_it_said(completed))
        )
    lines = completed.stdout.splitlines()
    if len(lines) != ROOT_USAGE_LINES:
        raise YoError(
            "cannot read the VM's root usage from df: expected %d lines, got '%s'"
            % (ROOT_USAGE_LINES, ui.shown(completed.stdout))
        )
    human_line, kib_line = lines[1], lines[3]
    fields = kib_line.split()
    total, used = df_kib(fields, 1), df_kib(fields, 2)
    if total is None or used is None:
        raise YoError(
            "cannot read the VM's root usage from df: no sizes in '%s'"
            % ui.shown(kib_line)
        )
    if total == 0:
        raise YoError(
            "cannot read the VM's root usage from df: it reports a total of 0 KiB in '%s'"
            % ui.shown(kib_line)
        )
    return RootUsage(human_line, total, used)
