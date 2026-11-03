"""The interface a host driver (Lima, libvirt) gives the rest of yo."""

from __future__ import annotations

from typing import List, Optional, Protocol, Sequence, Tuple


class Driver(Protocol):
    """The host side of the box: everything that differs between host OSes.

    main() picks one with driver_for() and set_box(), and nothing else asks
    which OS it runs on. Methods read the machine from box.name, like the rest
    of yo. Statuses use Lima's words: "Running" when the VM is up. Methods that
    return a command line or a message build a printed remedy.
    """

    host_os: str  # YOLOBOX_HOST_OS for nix: "darwin" or "linux"
    name: str  # what messages call the driver ("lima: ...")
    instances_dir: str  # instance directories, relative to $HOME
    start_command: str  # the background process unlock_disk watches
    gaps_are_fatal: bool  # vm_up refuses config_gaps() (True) or only warns

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
    def creation_refusal(self) -> Optional[str]: ...  # why start() will not create
    def config_gaps(self) -> List[str]: ...  # how the instance breaks guarantees
    def config_gap_message(self, gaps: Sequence[str]) -> str: ...  # with the fixes
    def host_gaps(self) -> List[str]: ...  # what the host breaks, each with its fix
    def verify_initrd_listener(self, port: int) -> None: ...  # YoError: not the VM's
    def start(self, passphrase: bytes) -> None: ...  # start, unlock, operator's ssh
    def stop(self) -> None: ...  # YoError when it will not stop
    def stop_command(self) -> str: ...
    def stop_failed(self, detail: str) -> str: ...  # first line of the abort
    def stop_recovery(self) -> List[str]: ...  # how to stop what stop() could not
    def delete_command(self) -> str: ...
    def list_command(self) -> str: ...  # shows the instance's state
    def boot_log(self) -> str: ...  # the VM's console log
    def show_status(self) -> None: ...  # the driver's view, head of `yo status`
    def disk_image(self) -> Optional[Tuple[str, int]]: ...  # label and host KiB
    def disk_ceiling_gib(self) -> int: ...  # YoError when unreadable
    def grow_disk(self, current_gib: int, wanted_gib: int) -> None: ...  # leaves VM off
