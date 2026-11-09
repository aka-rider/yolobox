"""Reverse forwards into the guest, the operator's ssh master that carries them, and the herdr machine."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import aws, proc, sshx, state, ui
from .config import (
    AGENT_AWS_BROKER_SOCK,
    AWS_BROKER_GUEST_SOCK,
    OP_GUEST_SOCK,
    SSH_CONFIG_INCLUDE_LINE,
)
from .driver import Removal
from .sshx import AGENT, OPERATOR
from .ui import YoError


def herdr_machine_id(listing_json: str) -> Optional[str]:
    """The id of the herdr machine whose target is this box's alias, or None.

    `herdr machine remove` takes that id, not the target. A matching entry
    without a usable id is an error, never a guess, for `yo bootstrap` too: it
    exists, but nothing can remove it.
    """
    try:
        entries = json.loads(listing_json)
    except ValueError:
        raise YoError("herdr machine list --json produced unparseable output")
    if not isinstance(entries, list):
        raise YoError("herdr machine list --json did not print a JSON array")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("target") == state.box.herdr_alias:
            machine_id = entry.get("id")
            if not isinstance(machine_id, str) or not machine_id:
                raise YoError(
                    "herdr machine list --json: the entry for %s has no id, so yo "
                    "cannot tell which machine it is. Inspect it with `herdr machine "
                    "list --json` and delete it by hand with `herdr machine remove "
                    "<id>`" % state.box.herdr_alias
                )
            return machine_id
    return None


def herdr_binary() -> Optional[str]:
    return shutil.which("herdr")


def listed_herdr_machine_id(herdr: str) -> Optional[str]:
    listing = proc.run([herdr, "machine", "list", "--json"], check=True)
    return herdr_machine_id(listing.stdout)


def herdr_machine_removals(herdr: str) -> List[Removal]:
    """The herdr machine registered for this box, as a step of `yo destroy`; none
    when herdr has no such machine."""
    machine_id = listed_herdr_machine_id(herdr)
    if machine_id is None:
        return []

    def remove(held: state.HeldBox) -> None:
        proc.run([herdr, "machine", "remove", machine_id], check=True)

    return [Removal("delete the herdr machine %s" % state.box.herdr_alias, remove)]


def ensure_herdr_machine(held: state.HeldBox, ssh_config_includes_the_vm: bool) -> None:
    """Under the box lock, listed and added in one hold: two bootstraps must
    not both add the machine."""
    herdr = herdr_binary()
    if herdr is None:
        ui.err(
            "yo: herdr not found on this host's PATH — skipping herdr machine registration"
        )
        return
    if listed_herdr_machine_id(herdr) is None:
        add_herdr_machine(herdr, ssh_config_includes_the_vm)


def add_herdr_machine(herdr: str, ssh_config_includes_the_vm: bool) -> None:
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
    alias = state.box.herdr_alias
    argv = [herdr, "machine", "add", alias, "--label", alias]
    proc.checked(proc.run(argv), shlex.join(argv))


def op_probe_alive(completed: subprocess.CompletedProcess) -> bool:
    if completed.returncode == 0:
        return True
    return (
        completed.returncode == 1 and "The agent has no identities" in completed.stdout
    )


def aws_broker_probe_alive(completed: subprocess.CompletedProcess) -> bool:
    return completed.returncode == 0


# How long the broker probe waits for an answer through the forward.
BROKER_PROBE_SECONDS = 5


@dataclass(frozen=True)
class ReverseForward:
    name: str
    guest_path: str
    host_path: str
    probe: Tuple[str, ...]
    alive: Callable[[subprocess.CompletedProcess], bool]


# `ssh-add -l` with SIGPIPE ignored, so a dead forward is ssh-add's own exit 1,
# never a death by SIGPIPE that ssh would report as 255.
OP_PROBE = ("sh", "-c", "trap '' PIPE; exec ssh-add -l")


def one_password_forward() -> ReverseForward:
    return ReverseForward(
        "1password",
        OP_GUEST_SOCK,
        state.driver().op_sock(),
        OP_PROBE,
        op_probe_alive,
    )


def aws_broker_forward() -> ReverseForward:
    return ReverseForward(
        "aws-broker",
        AWS_BROKER_GUEST_SOCK,
        state.aws_broker_sock(),
        (
            "curl",
            "-sS",
            "--max-time",
            str(BROKER_PROBE_SECONDS),
            "--unix-socket",
            AGENT_AWS_BROKER_SOCK,
            "http://broker/health",
        ),
        aws_broker_probe_alive,
    )


def forward_control_cmd(action: str, forward: ReverseForward) -> List[str]:
    return sshx.control_argv(
        OPERATOR, action, "-R", "%s:%s" % (forward.guest_path, forward.host_path)
    )


def forward_failure(forward: ReverseForward) -> Optional[str]:
    """None when the agent can use the forward; else what its probe said, for
    `yo status` to show. The one probe of a forward."""
    completed = sshx.ssh_run(AGENT, list(forward.probe))
    if forward.alive(completed):
        return None
    said = " ".join((completed.stderr or completed.stdout or "").split())
    return "its probe exited %d%s" % (
        completed.returncode,
        proc.said_suffix(ui.shown(said)),
    )


def ensure_session() -> None:
    """The operator's ssh master, carrying both reverse forwards, declared
    whole on every call: any master at the operator's ControlPath may have
    been opened by a session that asked for no forward (git, an editor, `yo
    gc`). A mux session opens the master when none answers (ControlMaster
    auto), then both forwards are asked for. A master answers a forward it
    already carries with success, and the guest's sshd runs with
    StreamLocalBindUnlink, so it replaces a socket a dead master left."""
    opener = ["true"]
    proc.checked(
        sshx.probe(OPERATOR, opener, mux=True), shlex.join(opener), "on the box"
    )
    for forward in (one_password_forward(), aws_broker_forward()):
        proc.run(forward_control_cmd("forward", forward), check=True)


def vm_services() -> bool:
    """The box's host-side services, whole: the master with its forwards, and
    the AWS broker serving today's allowlist (or stopped). True when the guest
    has AWS. They serve the agent, whose login the caller has checked."""
    ensure_session()
    return aws.ensure_aws_broker()
