"""The interface a host driver (Lima, libvirt) gives the rest of yo."""

from __future__ import annotations

import contextlib
import enum
import errno
import os
import socket
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from . import common, ui
from .config import GIB, LOOPBACK
from .ui import YoError

if TYPE_CHECKING:
    from .state import HeldBox


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


def size_flags(
    memory_gib: Optional[int], disk_gib: Optional[int], cpus: Optional[int]
) -> List[str]:
    """The flags that ask for a size, spelled the same by yo and limactl; None: not asked."""
    asked = (("--memory", memory_gib), ("--disk", disk_gib), ("--cpus", cpus))
    return [
        word
        for flag, value in asked
        if value is not None
        for word in (flag, str(value))
    ]


class NewBox(NamedTuple):
    """The size of a box start() creates: --memory and --disk as asked (None:
    the driver's default) and the CPU count, always decided (default_cpus)."""

    memory_gib: Optional[int]
    disk_gib: Optional[int]
    cpus: int

    def fill(self, memory_gib: int, disk_gib: int) -> BoxSize:
        """This size, with the driver's defaults for what was not asked."""
        return BoxSize(
            memory_gib if self.memory_gib is None else self.memory_gib,
            disk_gib if self.disk_gib is None else self.disk_gib,
            self.cpus,
        )

    def flags(self) -> List[str]:
        return size_flags(self.memory_gib, self.disk_gib, self.cpus)


class Size(NamedTuple):
    """What `--memory`, `--disk` and `--cpus` asked of a box, in GiB and CPUs; None: not asked.

    A box that does not exist is created at it; one that exists is refused them.
    """

    memory_gib: Optional[int]
    disk_gib: Optional[int]
    cpus: Optional[int] = None

    @property
    def asked(self) -> bool:
        """Whether any flag was given."""
        return any(value is not None for value in self)

    def for_creation(self) -> NewBox:
        """This size with the default CPU count when --cpus was not asked."""
        cpus = self.cpus if self.cpus is not None else default_cpus()
        return NewBox(self.memory_gib, self.disk_gib, cpus)

    def flags(self) -> List[str]:
        return size_flags(self.memory_gib, self.disk_gib, self.cpus)


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
    printed before a --yes run and as each step of it. Listing the pieces
    changes nothing; removing one runs under the box lock, which `remove` takes.
    """

    what: str
    remove: Callable[[HeldBox], None]


class VmState(enum.Enum):
    """Whether the VM holds its memory: up, off, or something in between that
    only the host tool's own word (VmStatus.word) describes."""

    RUNNING = "running"
    OFF = "off"
    OTHER = "other"


class VmStatus(NamedTuple):
    state: VmState
    word: str  # the host tool's own word, for messages ("Broken", "paused")


class SshFacts(NamedTuple):
    """How ssh reaches the box's sshd on this host's loopback as the operator:
    what yo writes into the instance's ssh.config, and what the initrd probes
    dial before that file exists."""

    port: int
    user: str
    identity_file: str
    identity_agent: str  # "none" when the identity is a plain key file


class DriverHints(NamedTuple):
    """The prose a driver lends to shared messages. `{name}` is the machine,
    `{vm_name}` the driver's name for it, `{dir}` its instance directory
    (state.hint fills them in)."""

    keepalive_name: str  # whose pid keepalive_pid() is ("host agent", "ssh master")
    login_fix: str  # what the operator fixes when the box refuses yo's login
    force_stop_command: str  # a hard power-off, to paste
    where_to_look: str  # what the host can show of a boot that went wrong


class Driver(Protocol):
    """The host side of the box: everything that differs between host OSes.

    main() picks one with driver_for() and set_box(), and nothing else asks
    which OS it runs on. Methods read the machine from box.name, like the rest
    of yo.
    """

    name: str  # what messages call the driver ("lima: ...")
    instances_dir: str  # instance directories, relative to $HOME
    hints: DriverHints

    def require_tool(self) -> None: ...  # YoError when the host lacks the tool
    def op_sock(self) -> str: ...  # the host's 1Password agent socket
    def ssh_facts(self) -> SshFacts: ...  # YoError when the instance does not exist
    def forward_port(self, guest_port: int) -> int: ...  # the host port forwarded
    # The pid the AWS broker lives with, read without starting anything: a box
    # with no master or agent yet answers None.
    def keepalive_pid(self) -> Optional[int]: ...
    def vm_status(self) -> Optional[VmStatus]: ...  # None: no such instance
    # Why start() will not create the instance, at that size.
    def creation_refusal(self, size: Size) -> Optional[str]: ...
    # Why start() will not start it on this host, read before anything changes.
    def start_refusal(self) -> Optional[str]: ...
    # The disk ceiling of the instance that exists, in whole GiB, read from the
    # host (YoError when it cannot be read).
    def disk_gib(self) -> int: ...
    def verify_initrd_listener(self, port: int) -> None: ...  # YoError: not the VM's
    # Create the instance at `new_box` from the seed image, stopped.
    def create(self, held: HeldBox, new_box: NewBox) -> None: ...
    # Start the instance, unlock its disk with `passphrase()` should an initrd
    # ask, and wait for the operator's ssh, under the box lock.
    def start(self, held: HeldBox, passphrase: Callable[[], bytes]) -> None: ...
    def stop(self, held: HeldBox) -> None: ...  # YoError when it will not stop
    def force_stop(self, held: HeldBox) -> None: ...  # a hard power-off
    # What of this machine the driver holds and `yo destroy` removes, in the order
    # to remove it. Only what exists is listed, so a machine with nothing left
    # gives [].
    def removals(self) -> List[Removal]: ...
    # The driver's view, head of `yo status`. A warning line when it could not
    # show it, which the report keeps and the rest of the report follows.
    def show_status(self) -> Optional[str]: ...
    def disk_image(self) -> Optional[Tuple[str, int]]: ...  # label and host KiB
    # Raise the disk's ceiling. The VM must be off, and it stays off.
    def grow_disk(self, held: HeldBox, wanted_gib: int) -> None: ...
    # The one remedy for a box that must be made anew, to paste: destroy it,
    # then bootstrap it at `size`, which creates it.
    def recreate_command(self, size: Size) -> str: ...


# In the directory of every instance: the lock that makes picking a host
# port and taking it one step.
PORT_LOCK_NAME = ".port.lock"


@contextlib.contextmanager
def port_lock(instances_dir: str) -> Iterator[None]:
    """Hold the host-wide lock in `instances_dir` that makes picking a port and
    taking it one step.

    Two `yo` processes would otherwise both pick the lowest free port, and
    one of the two would fail or, worse, land on the other's port.
    """
    with common.held_flock(os.path.join(instances_dir, PORT_LOCK_NAME), wait=True):
        yield


def loopback_port_bound(port: int) -> bool:
    """Whether something already holds 127.0.0.1:port (a bind probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError as failure:
            if failure.errno == errno.EADDRINUSE:
                return True
            raise YoError(
                "cannot probe %s:%d: %s" % (LOOPBACK, port, ui.os_error_reason(failure))
            )
    return False


def lowest_free_port(
    window: range, taken: Sequence[int], is_bound: Callable[[int], bool]
) -> int:
    for port in window:
        if port not in taken and not is_bound(port):
            return port
    raise YoError(
        "no free host port in %d-%d: every one is bound or belongs to a box; stop a "
        "box or end a forward" % (window.start, window[-1])
    )
