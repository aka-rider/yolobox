"""Calling yolobox-guest and the guest's accounts, host keys and units."""

from __future__ import annotations

import subprocess
from typing import List, NamedTuple, Optional, Sequence

from . import proc, release, sshconfig, sshx, state, ui
from .config import (
    AGENT_HOME,
    FILE_MODE_PRIVATE,
    HELPER,
    HELPER_NO_SUBCOMMAND,
    PROXY_SOCKET_UNITS,
)
from .sshx import AGENT, OPERATOR, SshRole
from .ui import YoError


HELPER_MARKER = "unknown subcommand"

HOST_KEY_GLOB = "/etc/ssh/ssh_host_*_key.pub"


def ensure_guest_known_hosts() -> None:
    # The herdr_alias_block's UserKnownHostsFile has to be pre-populated with
    # the guest's real key: herdr passes -o StrictHostKeyChecking=yes on the
    # command line for every saved machine, which overrides anything weaker
    # this alias sets, so "no known key yet" can never pass. The fetch is
    # first contact only while nothing is pinned; with a pin it is pinned
    # itself (sshx.host_key_fetch_role), so a changed key is refused, never
    # learned anew, and the file is rewritten only after a fetch that passed.
    current = proc.read_text_file(state.ssh_cfg(), errors="surrogateescape")
    if current is None:
        return
    host_block = sshconfig.parse_ssh_host_block(current, state.box.vm_host)
    if host_block is None:
        return

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

    lines = sshconfig.known_hosts_lines(host_block, completed.stdout)
    if not lines:
        raise YoError(
            "the guest reported no parseable ssh host keys at %s" % HOST_KEY_GLOB
        )

    proc.atomic_write(
        state.known_hosts_path(), "\n".join(lines) + "\n", FILE_MODE_PRIVATE
    )


def guest_helper_check(
    completed: subprocess.CompletedProcess, what: str
) -> subprocess.CompletedProcess:
    if completed.returncode != 0:
        raise YoError(
            proc.failed_message(
                what, completed.returncode, proc.failure_detail(completed)
            )
        )
    return completed


def guest_helper(
    role: SshRole,
    sub: str,
    args: Sequence[str] = (),
    *,
    stream: bool = False,
    **kwargs,
) -> subprocess.CompletedProcess:
    if stream:
        kwargs["capture"] = False
        kwargs["filtered"] = True
    completed = sshx.probe(role, [HELPER, sub, *args], **kwargs)
    # The exit code alone is never read as skew: a subcommand's own child can
    # exit 127 or 64 too. Both skews need their stderr marker, which a streamed
    # run also keeps (run_filtered returns stderr's tail).
    stderr = completed.stderr or ""
    if completed.returncode == 127 and "not found" in stderr:
        raise helper_skew_error(role, 127, "the box has no %s" % HELPER)
    if completed.returncode == HELPER_NO_SUBCOMMAND and HELPER_MARKER in stderr:
        raise helper_skew_error(
            role,
            HELPER_NO_SUBCOMMAND,
            "the box's %s has no '%s' subcommand" % (HELPER, sub),
        )
    return completed


def helper_skew_error(role: SshRole, code: int, what: str) -> YoError:
    """`what` is what the exit code and marker mean on an honest box; the message
    says whose session said so, since the agent's own session can say anything."""
    return YoError(
        release.helper_skew("%s (the %s's session exited %d)" % (what, role.name, code))
    )


def agent_paths(sub: str, args: Sequence[str] = (), *, check: bool = True) -> List[str]:
    completed = guest_helper(AGENT, sub, args, binary=True)
    if completed.returncode != 0:
        if check:
            guest_helper_check(completed, sub)
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


def vm_units_present() -> bool:
    return (
        sshx.probe(OPERATOR, ["systemctl", "cat", PROXY_SOCKET_UNITS[0]]).returncode
        == 0
    )


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
            "cannot read the VM's root usage from df: expected %d lines, got %r"
            % (ROOT_USAGE_LINES, completed.stdout)
        )
    human_line, kib_line = lines[1], lines[3]
    fields = kib_line.split()
    total, used = df_kib(fields, 1), df_kib(fields, 2)
    if total is None or used is None:
        raise YoError(
            "cannot read the VM's root usage from df: no sizes in %r" % kib_line
        )
    if total == 0:
        raise YoError(
            "cannot read the VM's root usage from df: it reports a total of 0 KiB in %r"
            % kib_line
        )
    return RootUsage(human_line, total, used)
