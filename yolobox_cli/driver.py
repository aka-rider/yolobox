"""The interface a host driver (Lima, libvirt) gives the rest of yo."""

from __future__ import annotations

import os
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from .config import GIB
from .ui import YoError

if TYPE_CHECKING:
    # Only for the annotation: agent imports state, which imports this module.
    from . import agent


class StopRefused(YoError):
    """stop() (or force_stop()) gave up without stopping the VM. `reason` says
    what happened; the message adds advice fit for the stop that raised it. A
    caller that knows more about the boot stage (bootstrap, right after
    nixos-rebuild ran in stage 2) prints `reason` with its own advice instead."""

    def __init__(self, reason: str, advice: str):
        super().__init__("%s. %s" % (reason, advice))
        self.reason = reason


# The most CPUs a new box gets when --cpus does not say; fewer on a small host.
DEFAULT_CPUS_CAP = 4


def host_cpu_count() -> int:
    """The host's CPU count, the one place yo asks; an error when it is unknown."""
    host = os.cpu_count()
    if host is None:
        raise YoError(
            "cannot read the host's CPU count (os.cpu_count() returned None), which "
            "the CPUs of a box are chosen against"
        )
    return host


def default_cpus() -> int:
    """The CPU count of a box created without --cpus: up to DEFAULT_CPUS_CAP, and
    always one CPU left to the host. Both drivers create a box with it."""
    cpus = min(DEFAULT_CPUS_CAP, host_cpu_count() - 1)
    if cpus < 1:
        raise YoError(
            "this host has only 1 CPU, which a box would take from the host "
            "entirely, so no box can be created here"
        )
    return cpus


class BoxSize(NamedTuple):
    """A whole box's size: memory and disk in GiB, and the vCPU count."""

    memory_gib: int
    disk_gib: int
    cpus: int

    def changes_to(self, wanted: "BoxSize") -> "Size":
        """What `wanted` changes of this size, as the flags that ask for it."""
        return Size(
            wanted.memory_gib if wanted.memory_gib != self.memory_gib else None,
            wanted.disk_gib if wanted.disk_gib != self.disk_gib else None,
            wanted.cpus if wanted.cpus != self.cpus else None,
        )

    def describe(self) -> str:
        return "%d GiB of memory, %d CPUs and a %d GiB disk" % (
            self.memory_gib,
            self.cpus,
            self.disk_gib,
        )


class Size(NamedTuple):
    """What `--memory`, `--disk` and `--cpus` asked of a box, in GiB and CPUs; None: not asked.

    A box that does not exist is created at it; one that exists and is stopped
    is resized to it (vm_up decides which).
    """

    memory_gib: Optional[int]
    disk_gib: Optional[int]
    cpus: Optional[int] = None

    @property
    def asked(self) -> bool:
        """Whether any flag was given."""
        return any(value is not None for value in self)

    def for_creation(self) -> "Size":
        """This size with the default CPU count when --cpus was not asked."""
        return self if self.cpus is not None else self._replace(cpus=default_cpus())

    def fill(self, base: BoxSize) -> BoxSize:
        """This size, with `base` for everything that was not asked."""
        return BoxSize(
            base.memory_gib if self.memory_gib is None else self.memory_gib,
            base.disk_gib if self.disk_gib is None else self.disk_gib,
            base.cpus if self.cpus is None else self.cpus,
        )

    def flags(self) -> List[str]:
        """The flags that ask for this size, spelled the same by yo and limactl."""
        asked = (
            ("--memory", self.memory_gib),
            ("--disk", self.disk_gib),
            ("--cpus", self.cpus),
        )
        return [
            word
            for flag, value in asked
            if value is not None
            for word in (flag, str(value))
        ]


NO_SIZE = Size(None, None, None)


def exact_gib(nbytes: int, what: str) -> int:
    """`nbytes` as whole GiB, the unit yo compares sizes in; an error for a
    fraction, which a comparison in whole GiB would round into a wrong answer."""
    gib, rest = divmod(nbytes, GIB)
    if rest:
        raise YoError(
            "the box's %s is %d bytes, which is not a whole number of GiB; yo "
            "sizes a box in whole GiB, so set it to a whole number first"
            % (what, nbytes)
        )
    return gib


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
    keepalive_name: str  # whose pid keepalive_pid() is ("host agent", "ssh master")
    instances_dir: str  # instance directories, relative to $HOME
    start_command: str  # the background process unlock_disk watches
    off_status: str  # what vm_status() says of a VM that is stopped

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
    # The same pid, read without starting anything: a box with no master or
    # agent yet answers None. `yo status` reports and never repairs (YoError when
    # it cannot be read).
    def running_keepalive_pid(self) -> Optional[int]: ...
    def keepalive_pid_error(self) -> str: ...  # why keepalive_pid() was empty
    def vm_status(self) -> Optional[str]: ...  # None: no such instance
    # Why start() will not create the instance, at that size.
    def creation_refusal(self, size: Size) -> Optional[str]: ...
    # The size of the instance that exists, read from the host (YoError when it
    # cannot be read).
    def current_size(self) -> BoxSize: ...
    # Why the stopped instance cannot take `wanted`, or None. Read-only, so vm_up
    # can run it before the passphrase.
    def resize_refusal(self, current: BoxSize, wanted: BoxSize) -> Optional[str]: ...
    # Set the stopped instance's memory and CPUs, taking effect at its next start.
    def resize(self, memory_gib: int, cpus: int) -> None: ...
    def config_gaps(self) -> List[str]: ...  # how the instance breaks guarantees
    # What yo cannot verify about the instance, each a full sentence: no gap, so
    # `yo status` shows it without failing and `yo up` does not repeat it.
    def config_notes(self) -> List[str]: ...
    # Those of `gaps` vm_up refuses to start with; it only warns about the rest.
    def fatal_gaps(self, gaps: Sequence[str]) -> List[str]: ...
    def config_gap_message(self, gaps: Sequence[str]) -> str: ...  # with the fixes
    def host_gaps(self) -> List[str]: ...  # what the host breaks, each with its fix
    def verify_initrd_listener(self, port: int) -> None: ...  # YoError: not the VM's
    # Start, unlock, operator's ssh. `size` is for a start that creates the
    # instance; vm_up passes NO_SIZE for one that exists.
    def start(self, passphrase: agent.Passphrase, size: Size) -> None: ...
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
    # The driver's view, head of `yo status`. A warning line when it could not
    # show it, which the report keeps and the rest of the report follows.
    def show_status(self) -> Optional[str]: ...
    def disk_image(self) -> Optional[Tuple[str, int]]: ...  # label and host KiB
    # Raise the disk's ceiling. The VM must be off, and it stays off.
    def grow_disk(self, current_gib: int, wanted_gib: int) -> None: ...
