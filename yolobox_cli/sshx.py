"""The ssh roles and the one place that builds an ssh command line for the guest."""

from __future__ import annotations

import enum
import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

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


# getent's exit code for a key the database does not hold.
GETENT_KEY_NOT_FOUND = 2

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
    forward. The way out of a stale pin is to delete the file (docs/ssh.md).
    """
    pin = proc.read_text_file(state.known_hosts_path(), errors="surrogateescape")
    if pin is None or not pin.strip():
        return OPERATOR_FIRST_CONTACT
    return OPERATOR


def ssh_options(role: SshRole, mux: bool = False) -> List[str]:
    """The options every ssh and scp that yo spawns for the guest starts with."""
    sshconfig.ensure_agent_ssh_config()
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
    exit_run = proc.run(
        ssh_argv(role, "-O", "exit", state.box.vm_host, mux=True), guest=role.name
    )
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
    """ssh could not reach the box at all (exit 255), as opposed to a command that failed there."""


def unreachable(role: SshRole, said: str = "") -> Unreachable:
    """The role's session exited 255, with what ssh printed (`said`), if anything.
    The message says whose session and which code, since a command the agent runs
    on the box can exit 255 too: the reader can then tell the two apart."""
    message = state.not_up_hint(
        "cannot reach %s as %s (the %s's ssh session exited %d, ssh's own code for "
        "no connection)" % (state.box.name, role.name, role.name, SSH_UNREACHABLE)
    )
    block = ui.quoted_block(said)
    return Unreachable("%s\n%s" % (message, block) if block else message)


def transport_failed(completed: subprocess.CompletedProcess) -> bool:
    """Whether ssh itself failed to connect, as opposed to the command it ran.

    ssh exits 255 for both: when it cannot connect, and when the command on the
    box died of a signal (sshd reports an exit-signal, and ssh has no exit code
    to pass on). Only the first has ssh's own words on stderr (`Connection
    refused`, `Permission denied`, a changed host key); a command killed by a
    signal leaves ssh silent. So 255 with nothing said is a command that died,
    and only 255 with a reason is the box out of reach (docs/ssh.md#healing-
    reverse-forwards: a probe killed by SIGPIPE was once read as a lost box).
    """
    return completed.returncode == SSH_UNREACHABLE and bool(
        (completed.stderr or "").strip()
    )


def probe(role: SshRole, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """ssh_run for a caller that reads the exit code as a fact about the box.

    ssh itself exits 255 when it cannot connect, so that code says nothing about
    the command: it raises Unreachable before anyone interprets it
    (transport_failed tells that 255 from a command's death by signal).
    """
    completed = ssh_run(role, argv, **kwargs)
    if transport_failed(completed):
        raise unreachable(role, completed.stderr)
    return completed


def guest_out(argv: Sequence[str]) -> str:
    """What a command printed on the box. Unreachable when the box is lost, YoError with
    the command's stderr when it failed there: its output is a fact only if it succeeded."""
    completed = probe(OPERATOR, argv)
    if completed.returncode != 0:
        raise box_failure(shlex.join(argv), completed)
    return completed.stdout


def box_failure(what: str, completed: subprocess.CompletedProcess) -> YoError:
    """A command that ran on the box and failed there, with what it said."""
    return YoError(
        proc.failed_message(
            what, completed.returncode, proc.failure_detail(completed), "on the box"
        )
    )


class AccountStatus(enum.Enum):
    AGENT = "agent"
    # The box has no agent account, a fact `getent` states.
    OPERATOR_ONLY = "operator_only"
    # The agent exists but its login fails (its own rc file exits, say).
    AGENT_FAILING = "agent_failing"
    UNREACHABLE = "unreachable"


def account_status() -> Tuple[AccountStatus, str]:
    """Which accounts work, as (status, detail).

    The detail of AGENT_FAILING is the failed probe's exit code and stderr, and that
    of UNREACHABLE what ssh said, both as display text (a quoted block); the others
    have none.
    """
    agent_probe = ssh_run(AGENT, ["true"])
    if agent_probe.returncode == 0:
        return AccountStatus.AGENT, ""
    # The operator's probe is judged on its own: ssh's 255 is the box out of
    # reach, with what that session said; `true` failing there is a box that
    # answers but is broken (an rc file that exits), not one that is away.
    operator_probe = ssh_run(OPERATOR, ["true"])
    if transport_failed(operator_probe):
        return AccountStatus.UNREACHABLE, ui.quoted_block(operator_probe.stderr)
    if operator_probe.returncode != 0:
        raise box_failure("ssh %s true" % state.box.vm_host, operator_probe)
    lookup = ssh_run(OPERATOR, ["getent", "passwd", AGENT_USER])
    if transport_failed(lookup):
        return AccountStatus.UNREACHABLE, ui.quoted_block(lookup.stderr)
    if lookup.returncode == GETENT_KEY_NOT_FOUND:
        return AccountStatus.OPERATOR_ONLY, ""
    if lookup.returncode != 0:
        raise box_failure("getent passwd %s" % AGENT_USER, lookup)
    return AccountStatus.AGENT_FAILING, proc.failed_message(
        "ssh %s@%s true" % (AGENT_USER, state.box.vm_host),
        agent_probe.returncode,
        proc.failure_detail(agent_probe),
    )


def require_agent_account() -> None:
    status, detail = account_status()
    if status is AccountStatus.AGENT:
        return
    if status is AccountStatus.AGENT_FAILING:
        ui.err("yo: cannot ssh into %s as '%s'." % (state.box.name, AGENT_USER))
        ui.err("    The account exists, but its login fails:\n%s" % detail)
        ui.err(
            "    Its shell startup files (~/.bashrc, ~/.profile) are the usual cause; "
            "inspect them as the operator: %s" % state.yo_cmd("ssh")
        )
    elif status is AccountStatus.OPERATOR_ONLY:
        ui.err("yo: cannot ssh into %s as '%s'." % (state.box.name, AGENT_USER))
        ui.err(
            "    Every AI session runs as that account, and this VM has no working one —"
        )
        ui.err("    it predates the operator/agent split. Rebuild it:")
        ui.err("        %s" % state.destroy_then_bootstrap_command())
    else:
        if detail:
            ui.err(detail)
        ui.err(
            "yo: cannot reach %s as operator either — is the VM up? %s"
            % (state.box.name, state.yo_cmd("status"))
        )
    raise YoError(None, code=1)
