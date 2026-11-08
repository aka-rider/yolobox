"""The ssh roles and the one place that builds an ssh command line for the guest."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import proc, sshconfig, state, ui
from .config import AGENT_USER, SSH_UNREACHABLE
from .driver import Removal
from .sshconfig import FIRST_CONTACT_OPTS
from .ui import YoError


@dataclass(frozen=True)
class SshRole:
    user: Optional[str]
    own_control_path: bool
    pins_host_key: bool = True

    @property
    def name(self) -> str:
        """The account the role logs in as, for a message: "operator" or the agent's."""
        return self.user or "operator"


OPERATOR = SshRole(None, False)
AGENT = SshRole(AGENT_USER, True)
# Only for the first fetch of the guest's host keys (host_key_fetch_role):
# with nothing pinned yet there is nothing to check a key against.
OPERATOR_FIRST_CONTACT = SshRole(None, False, pins_host_key=False)


def host_key_fetch_role() -> SshRole:
    """The role ensure_guest_known_hosts fetches the guest's host keys with.

    First contact only while nothing is pinned: a fresh instance, or a file
    emptied by hand. Once a pin exists the fetch is pinned too, so a key that
    changed under a kept known_hosts (a VM recreated behind yo's back, or
    something else holding the port) is ssh's own REMOTE HOST IDENTIFICATION
    HAS CHANGED refusal, never learned anew and then trusted with the agent
    forward. The way out of a stale pin is to delete the file.
    """
    pin = proc.read_text_file(state.known_hosts_path(), errors="surrogateescape")
    if pin is None or not pin.strip():
        return OPERATOR_FIRST_CONTACT
    return OPERATOR


def ssh_options(role: SshRole, mux: bool = False) -> List[str]:
    """The options every ssh and scp that yo spawns for the guest starts with."""
    state.require_control_path_fits()
    argv = ["-F", state.ssh_cfg()]
    if not role.pins_host_key:
        argv += FIRST_CONTACT_OPTS
    if role.user is not None:
        argv += sshconfig.ssh_opt("User", role.user)
    if not mux:
        argv += sshconfig.ssh_opt("ControlPath", "none")
    elif role.own_control_path:
        argv += sshconfig.ssh_opt("ControlPath", state.agent_control_path())
    return argv


def ssh_argv(role: SshRole, *tail: str, mux: bool = False) -> List[str]:
    """`ssh` with the role's options, then `tail`: the one place an ssh command starts."""
    return ["ssh"] + ssh_options(role, mux) + list(tail)


def master_alive(role: SshRole) -> bool:
    return (
        proc.run(ssh_argv(role, "-O", "check", state.box.vm_host, mux=True)).returncode
        == 0
    )


def stop_master(role: SshRole, failure: str) -> None:
    """Ask the role's ssh master to quit, and raise `failure` (then ssh's stderr)
    when one still answers. What counts is that none is left: a master that died
    on its own since the caller looked makes the exit fail, and that is fine."""
    exit_run = proc.run(ssh_argv(role, "-O", "exit", state.box.vm_host, mux=True))
    if master_alive(role):
        said = ui.quoted_block(exit_run.stderr)
        raise YoError(
            "%s:\n%s" % (failure, said) if said else "%s (ssh said nothing)" % failure
        )


def agent_master_removals() -> List[Removal]:
    """Closing the agent account's ssh master, if one runs. The agent role's mux
    sessions open it (ControlMaster auto, ControlPersist yes) on both drivers,
    at a socket inside the instance directory, so it must quit before that
    directory goes."""
    if not master_alive(AGENT):
        return []

    def close() -> None:
        stop_master(
            AGENT,
            "%s's agent ssh master is still running after `ssh -O exit`"
            % state.box.name,
        )

    return [Removal("close the agent's ssh master", close)]


def forward_agent_opts() -> List[str]:
    return sshconfig.ssh_opt("ForwardAgent", state.driver().op_sock())


def ssh_run(
    role: SshRole,
    argv: Sequence[str],
    *,
    stdin: Optional[bytes] = None,
    env: Optional[Dict[str, str]] = None,
    mux: bool = False,
    capture: bool = True,
    check: bool = False,
    merge_stderr: bool = False,
    binary: bool = False,
    filtered: bool = False,
) -> subprocess.CompletedProcess:
    no_stdin = ["-n"] if stdin is None else []
    return proc.run(
        ssh_argv(role, *no_stdin, state.box.vm_host, shlex.join(argv), mux=mux),
        capture=capture,
        check=check,
        stdin=stdin,
        env=env,
        merge_stderr=merge_stderr,
        binary=binary,
        filtered=filtered,
        guest=role.name,
    )


class Unreachable(YoError):
    """ssh could not reach the box (exit 255), as opposed to a command that failed there."""


def unreachable(role: SshRole, said: Optional[str] = None) -> Unreachable:
    """The role's session exited 255, with what ssh printed (`said`); None when
    its stderr was streamed to the terminal instead. The message says whose
    session and which code, since a command the agent runs on the box can exit
    255 too: the reader can then tell the two apart."""
    message = state.not_up_hint(
        "cannot reach %s as %s (the %s's ssh session exited %d, ssh's own code for "
        "no connection)" % (state.box.name, role.name, role.name, SSH_UNREACHABLE)
    )
    block = ui.quoted_block(said or "")
    return Unreachable("%s\n%s" % (message, block) if block else message)


def probe(role: SshRole, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """ssh_run for a caller that reads the exit code as the command's. ssh's own
    255 is raised as Unreachable first. ssh says why whenever it fails, so a 255
    with an empty stderr is the command's own: it exited 255, or a signal ended it."""
    completed = ssh_run(role, argv, **kwargs)
    if completed.returncode == SSH_UNREACHABLE and completed.stderr != "":
        raise unreachable(role, completed.stderr)
    return completed


def guest_out(argv: Sequence[str], **kwargs) -> str:
    """What a command printed on the box. Unreachable when the box is lost, YoError with
    the command's stderr when it failed there: its output is a fact only if it succeeded."""
    return proc.checked(
        probe(OPERATOR, argv, **kwargs), shlex.join(argv), "on the box"
    ).stdout


def agent_login_problem() -> Optional[str]:
    """Why the agent account cannot be used, or None when `true` runs as it.

    The seed image has no agent account, and a broken rc file exits: both
    read as one problem, with one remedy. A refused agent is ssh's 255 like a
    lost box, so the operator's login tells the two apart, and Unreachable
    is raised only when the operator cannot log in either."""
    completed = ssh_run(AGENT, ["true"])
    if completed.returncode == 0:
        return None
    if completed.returncode == SSH_UNREACHABLE:
        probe(OPERATOR, ["true"])
    return (
        "%s has no working agent account: not bootstrapped yet, or broken; run %s\n%s"
        % (
            state.box.name,
            state.yo_cmd("bootstrap"),
            proc.failed_message(
                "ssh %s@%s true" % (AGENT_USER, state.box.vm_host),
                completed.returncode,
                proc.failure_detail(completed),
            ),
        )
    )


def require_agent_account() -> None:
    problem = agent_login_problem()
    if problem is not None:
        raise YoError(problem)
