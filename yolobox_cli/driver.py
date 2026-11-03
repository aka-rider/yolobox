"""The interface a host driver (Lima, libvirt) gives the rest of yo."""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Protocol, Sequence, Tuple


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
    def delete_command(self) -> str: ...
    def list_command(self) -> str: ...  # shows the instance's state
    def boot_log(self) -> str: ...  # the VM's console log
    # Why a VM that yo waits for is in `status`, not Running (None: it no longer
    # exists), and the way back.
    def vm_lost_hint(self, status: Optional[str]) -> str: ...
    def show_status(self) -> None: ...  # the driver's view, head of `yo status`
    def disk_image(self) -> Optional[Tuple[str, int]]: ...  # label and host KiB
    def disk_ceiling_gib(self) -> int: ...  # YoError when unreadable
    def grow_disk(self, current_gib: int, wanted_gib: int) -> None: ...  # leaves VM off
