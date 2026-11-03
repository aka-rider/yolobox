"""Reverse forwards into the guest, the herdr machine and the per-command healing of both."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import aws, guest, proc, sshx, state, ui
from .config import (
    AGENT_AWS_BROKER_SOCK,
    AWS_BROKER_GUEST_SOCK,
    OP_GUEST_SOCK,
    SSH_CONFIG_INCLUDE_LINE,
)
from .sshx import AGENT, OPERATOR
from .ui import YoError


def herdr_machine_exists(listing_json: str) -> bool:
    try:
        entries = json.loads(listing_json)
    except ValueError:
        raise YoError("herdr machine list --json produced unparseable output")
    if not isinstance(entries, list):
        raise YoError("herdr machine list --json did not print a JSON array")
    return any(
        isinstance(entry, dict) and entry.get("target") == state.box.herdr_alias
        for entry in entries
    )


def ensure_herdr_machine(ssh_config_includes_the_vm: bool) -> None:
    herdr = shutil.which("herdr")
    if herdr is None:
        ui.err(
            "yo: herdr not found on this host's PATH — skipping herdr machine registration"
        )
        return
    listing = proc.run([herdr, "machine", "list", "--json"], check=True)
    if herdr_machine_exists(listing.stdout):
        return
    if not ssh_config_includes_the_vm:
        # herdr runs `ssh <alias>`, and the alias exists only in the file that
        # ~/.ssh/config includes; without the line ssh cannot resolve it.
        ui.err(
            "yo: not saving the herdr machine %s: ~/.ssh/config does not reach the VM's"
            " ssh config, so herdr cannot resolve the host." % state.box.herdr_alias
        )
        ui.err(
            "    Add this line to the top of ~/.ssh/config, then run %s again:"
            % state.yo_cmd("bootstrap")
        )
        ui.err("        %s" % SSH_CONFIG_INCLUDE_LINE)
        return
    added = proc.run(
        [
            herdr,
            "machine",
            "add",
            state.box.herdr_alias,
            "--label",
            state.box.herdr_alias,
        ]
    )
    if added.returncode != 0:
        raise YoError(
            "herdr machine add %s failed: %s"
            % (state.box.herdr_alias, (added.stderr or added.stdout).strip())
        )


def op_probe_alive(completed: subprocess.CompletedProcess) -> bool:
    if completed.returncode == 0:
        return True
    return (
        completed.returncode == 1 and "The agent has no identities" in completed.stdout
    )


def aws_broker_probe_alive(completed: subprocess.CompletedProcess) -> bool:
    return completed.returncode == 0


@dataclass(frozen=True)
class ReverseForward:
    name: str
    guest_path: str
    host_path: str
    probe: Tuple[str, ...]
    alive: Callable[[subprocess.CompletedProcess], bool]


def one_password_forward() -> ReverseForward:
    return ReverseForward(
        "1password",
        OP_GUEST_SOCK,
        state.driver().op_sock(),
        ("ssh-add", "-l"),
        op_probe_alive,
    )


def aws_broker_forward() -> ReverseForward:
    return ReverseForward(
        "aws-broker",
        AWS_BROKER_GUEST_SOCK,
        aws.aws_broker_sock(),
        ("curl", "-sS", "--unix-socket", AGENT_AWS_BROKER_SOCK, "http://broker/health"),
        aws_broker_probe_alive,
    )


def forward_control_cmd(action: str, forward: ReverseForward) -> List[str]:
    return sshx.ssh_argv(
        OPERATOR,
        "-O",
        action,
        "-R",
        "%s:%s" % (forward.guest_path, forward.host_path),
        state.box.vm_host,
        mux=True,
    )


def forward_is_up(forward: ReverseForward) -> bool:
    """Whether the agent can use the forward: the one probe of it, here and in `yo status`."""
    return forward.alive(sshx.ssh_run(AGENT, list(forward.probe)))


def heal_reverse_forward(forward: ReverseForward) -> bool:
    if forward_is_up(forward):
        return True
    proc.run(forward_control_cmd("cancel", forward), check=False)
    sshx.ssh_run(OPERATOR, ["rm", "-f", forward.guest_path], mux=True, check=True)
    proc.run(forward_control_cmd("forward", forward), check=True)
    return forward_is_up(forward)


def heal_forward_collecting_errors(forward: ReverseForward, errors: List[str]) -> None:
    try:
        healed = heal_reverse_forward(forward)
    except YoError as failure:
        errors.append(
            "the %s forward into the VM failed while healing it: %s"
            % (forward.name, failure.message or "unknown error")
        )
        return
    if not healed:
        errors.append(
            "the %s forward into the VM is still dead after re-issuing it"
            % forward.name
        )


def ensure_forwards(aws_enabled: bool) -> List[str]:
    errors: List[str] = []
    if not os.path.exists(state.driver().op_sock()):
        ui.err(
            "yo: 1Password not running on this host (no socket at %s) — skipping its VM forward"
            % state.driver().op_sock()
        )
    else:
        heal_forward_collecting_errors(one_password_forward(), errors)
    if aws_enabled:
        heal_forward_collecting_errors(aws_broker_forward(), errors)
    return errors


def units_missing_note() -> str:
    return "box predates the 1Password/AWS forwards; run `%s`" % state.yo_cmd(
        "bootstrap"
    )


def vm_services() -> None:
    status, _ = sshx.account_status()
    if status == "unreachable":
        raise sshx.unreachable(OPERATOR)
    if not guest.vm_units_present():
        ui.err("yo: %s" % units_missing_note())
        return

    errors: List[str] = []
    disable_failure: Optional[str] = None
    aws_enabled = False
    try:
        aws_enabled = aws.ensure_aws_broker()
    except aws.BrokerDisableError as failure:
        disable_failure = failure.message or "aws-broker could not be disabled"
    except YoError as failure:
        errors.append(failure.message or "aws-broker failed")

    errors += ensure_forwards(aws_enabled)

    # Only the errors a fresh ssh master may cure get its remedy.
    reports: List[str] = []
    if errors:
        reports.append(
            "; ".join(errors)
            + " — recreate the master: %s && %s"
            % (state.driver().stop_command(), state.yo_cmd("up"))
        )
    if disable_failure is not None:
        reports.append(disable_failure)
    if reports:
        raise YoError("\n".join(reports))
