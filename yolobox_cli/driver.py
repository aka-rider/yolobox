"""The interface a host driver (Lima, libvirt) gives the rest of yo."""

from __future__ import annotations

from typing import Callable, List, NamedTuple, Optional, Protocol, Sequence, Tuple

from .ui import YoError


class StopRefused(YoError):
    """stop() (or force_stop()) gave up without stopping the VM. `reason` says
    what happened; the message adds advice fit for the stop that raised it. A
    caller that knows more about the boot stage (bootstrap, right after
    nixos-rebuild ran in stage 2) prints `reason` with its own advice instead."""

    def __init__(self, reason: str, advice: str):
        super().__init__("%s. %s" % (reason, advice))
        self.reason = reason


class Size(NamedTuple):
    """What `--memory` and `--disk` asked of a box yo creates, in GiB; None: not asked.

    A size applies only at creation: vm_up refuses one for a box that exists.
    """

    memory_gib: Optional[int]
    disk_gib: Optional[int]

    @property
    def asked(self) -> bool:
        """Whether either flag was given."""
        return self.memory_gib is not None or self.disk_gib is not None

    def flags(self) -> List[str]:
        """The flags that ask for this size, spelled the same by yo and `limactl create`."""
        asked = (("--memory", self.memory_gib), ("--disk", self.disk_gib))
        return [
            word for flag, gib in asked if gib is not None for word in (flag, str(gib))
        ]


NO_SIZE = Size(None, None)


class Removal(NamedTuple):
    """One piece of a machine `yo destroy` removes: what it is, and the way to remove it.

    `what` reads as an action ("delete volume X"), because the same line is
    printed before a --yes run and as each step of it.
    """

    what: str
    remove: Callable[[], None]


# The status of a VM that is up, in Lima's word, which every driver reports.
RUNNING = "Running"


class Driver(Protocol):
    """The host side of the box: everything that differs between host OSes.

    main() picks one with driver_for() and set_box(), and nothing else asks
    which OS it runs on. Methods read the machine from box.name, like the rest
    of yo. Statuses use Lima's words: "Running" when the VM is up. Methods that
    return a command line or a message build a printed remedy.
    """

    name: str  # what messages call the driver ("lima: ...")
    instances_dir: str  # instance directories, relative to $HOME
    start_command: str  # the background process unlock_disk watches

    @property
    def initrd_identity(self) -> str: ...  # $HOME-relative key of pre-unlock probes

    def require_tool(self) -> None: ...  # YoError when the host lacks the tool
    def op_sock(self) -> str: ...  # the host's 1Password agent socket
    def initrd_identity_agent(self) -> str: ...  # "none", or an agent socket
    # YoError naming the agent when the key the box's logins use is not on offer.
    # vm_up runs it before any start or unlock, main() for the other commands
    # that must log in, so a dead agent is never misread as an unreachable box;
    # `yo status` reports it as a line, `yo down` only when it cannot tell an
    # initrd from stage 2, and ensure_master only to open a master.
    def require_login_key(self) -> None: ...
    # What the operator fixes when the box refuses yo's login, for a message.
    def login_fix(self) -> str: ...
    # What a person can do about BootStage.UNKNOWN once the login is right.
    def unknown_next_step(self) -> str: ...
    def ssh_port(self) -> int: ...  # loopback port reaching sshd, initrd included
    def forward_port(self, guest_port: int) -> int: ...  # the host port forwarded
    def keepalive_pid(self) -> Optional[int]: ...  # pid the AWS broker lives with
    def keepalive_pid_error(self) -> str: ...  # why keepalive_pid() was empty
    def vm_status(self) -> Optional[str]: ...  # None: no such instance
    # Why start() will not create the instance, at that size.
    def creation_refusal(self, size: Size) -> Optional[str]: ...
    # Why an asked size cannot apply to the instance that exists, and the way out.
    def existing_size_refusal(self, size: Size) -> str: ...
    def config_gaps(self) -> List[str]: ...  # how the instance breaks guarantees
    # Those of `gaps` vm_up refuses to start with; it only warns about the rest.
    def fatal_gaps(self, gaps: Sequence[str]) -> List[str]: ...
    def config_gap_message(self, gaps: Sequence[str]) -> str: ...  # with the fixes
    def host_gaps(self) -> List[str]: ...  # what the host breaks, each with its fix
    def verify_initrd_listener(self, port: int) -> None: ...  # YoError: not the VM's
    # Start, unlock, operator's ssh. A start that creates uses `size`; any other
    # start raises for an asked size rather than drop it.
    def start(self, passphrase: bytes, size: Size) -> None: ...
    def stop(self) -> None: ...  # YoError when it will not stop
    def force_stop(self) -> None: ...  # a hard power-off, for an initrd box
    def force_stop_command(self) -> str: ...  # what force_stop() does, to paste
    def stop_command(self) -> str: ...
    def stop_failed(self, detail: str) -> str: ...  # first line of the abort
    def stop_recovery(self) -> List[str]: ...  # how to stop what stop() could not
    # What of this machine the driver holds and `yo destroy` removes, in the order
    # to remove it. Only what exists is listed, so a machine with nothing left
    # gives [].
    def removals(self) -> List[Removal]: ...
    def list_command(self) -> str: ...  # shows the instance's state
    # Where to look when the initrd's sshd never answered, from what the host
    # can actually show of the boot.
    def initrd_unseen_hint(self) -> str: ...
    # Why a VM that yo waits for is in `status`, not Running (None: it no longer
    # exists), and the way back.
    def vm_lost_hint(self, status: Optional[str]) -> str: ...
    def show_status(self) -> None: ...  # the driver's view, head of `yo status`
    def disk_image(self) -> Optional[Tuple[str, int]]: ...  # label and host KiB
    def disk_ceiling_gib(self) -> int: ...  # YoError when unreadable
    def grow_disk(self, current_gib: int, wanted_gib: int) -> None: ...  # leaves VM off
