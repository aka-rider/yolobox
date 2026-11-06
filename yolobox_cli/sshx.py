"""The ssh roles and the one place that builds an ssh command line for the guest."""

from __future__ import annotations

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


# getent's exit code for a key the database does not hold.
GETENT_KEY_NOT_FOUND = 2

OPERATOR = SshRole(None, False)
AGENT = SshRole(AGENT_USER, True)
# Only for fetching the guest's host keys to pin them (ensure_guest_known_hosts):
# the pin cannot be checked against itself, and after a VM recreated behind
# yo's back the old pin would refuse the very connection that replaces it.
OPERATOR_FIRST_CONTACT = SshRole(None, False, pins_host_key=False)


def ssh_options(role: SshRole, mux: bool = False) -> List[str]:
    """The options every ssh and scp that yo spawns for the guest starts with."""
    sshconfig.ensure_agent_ssh_config()
    argv = ["-F", state.ssh_cfg()]
    if not role.pins_host_key:
        argv += FIRST_CONTACT_OPTS
    if role.user is not None:
        argv += ["-o", "User=%s" % role.user]
    if not mux:
        argv += ["-o", "ControlPath=none"]
    elif role.own_control_path:
        argv += ["-o", "ControlPath=%s" % state.agent_control_path()]
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
        raise YoError("%s: %s" % (failure, exit_run.stderr.strip() or "no output"))


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
    return ["-o", 'ForwardAgent="%s"' % state.driver().op_sock()]


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
    inherit_stdin: bool = False,
    binary: bool = False,
    filtered: bool = False,
) -> subprocess.CompletedProcess:
    no_stdin = ["-n"] if stdin is None and not inherit_stdin else []
    return proc.run(
        ssh_argv(role, *no_stdin, state.box.vm_host, shlex.join(argv), mux=mux),
        capture=capture,
        check=check,
        stdin=stdin,
        env=env,
        merge_stderr=merge_stderr,
        binary=binary,
        filtered=filtered,
    )


class Unreachable(YoError):
    """ssh could not reach the box at all (exit 255), as opposed to a command that failed there."""


def unreachable(role: SshRole) -> Unreachable:
    return Unreachable(
        state.not_up_hint(
            "cannot reach %s as %s" % (state.box.name, role.user or "operator")
        )
    )


def probe(role: SshRole, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """ssh_run for a caller that reads the exit code as a fact about the box.

    ssh itself exits 255 when it cannot connect, so that code says nothing about
    the command: it raises Unreachable before anyone interprets it.
    """
    completed = ssh_run(role, argv, **kwargs)
    if completed.returncode == SSH_UNREACHABLE:
        raise unreachable(role)
    return completed


def guest_out(argv: Sequence[str]) -> str:
    """What a command printed on the box. Unreachable when the box is lost, YoError with
    the command's stderr when it failed there: its output is a fact only if it succeeded."""
    completed = probe(OPERATOR, argv)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise YoError(
            "%s failed on the box (exit %d)%s"
            % (
                shlex.join(argv),
                completed.returncode,
                ": " + stderr if stderr else "",
            )
        )
    return completed.stdout


def account_status() -> Tuple[str, str]:
    """Which accounts work, as (status, detail).

    `operator_only` means the box has no agent account, a fact `getent` states.
    An agent whose login merely fails (its own rc file exits, say) is `agent_failing`,
    with the failed probe's exit code and stderr as detail.
    """
    agent_probe = ssh_run(AGENT, ["true"])
    if agent_probe.returncode == 0:
        return "agent", ""
    if ssh_run(OPERATOR, ["true"]).returncode != 0:
        return "unreachable", agent_probe.stderr.rstrip("\n")
    lookup = ssh_run(OPERATOR, ["getent", "passwd", AGENT_USER])
    if lookup.returncode == SSH_UNREACHABLE:
        return "unreachable", lookup.stderr.rstrip("\n")
    if lookup.returncode == GETENT_KEY_NOT_FOUND:
        return "operator_only", ""
    if lookup.returncode != 0:
        raise YoError(
            "getent passwd %s failed on the box (exit %d)%s"
            % (AGENT_USER, lookup.returncode, ": " + lookup.stderr.strip())
        )
    stderr = agent_probe.stderr.strip()
    return "agent_failing", "ssh %s@%s true exited %d%s" % (
        AGENT_USER,
        state.box.vm_host,
        agent_probe.returncode,
        ": " + stderr if stderr else " with no stderr",
    )


def require_agent_account() -> None:
    status, detail = account_status()
    if status == "agent":
        return
    if status == "agent_failing":
        ui.err("yo: cannot ssh into %s as '%s'." % (state.box.name, AGENT_USER))
        ui.err("    The account exists, but its login fails: %s" % detail)
        ui.err(
            "    Its shell startup files (~/.bashrc, ~/.profile) are the usual cause; "
            "inspect them as the operator: %s" % state.yo_cmd("ssh")
        )
    elif status == "operator_only":
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
