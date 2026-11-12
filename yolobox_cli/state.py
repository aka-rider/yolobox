"""The machine yo drives, the paths under the user's home that belong to it, and remedy commands."""

from __future__ import annotations

import contextlib
import os
import pwd
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence, Set

from . import common, proc, ui
from .config import DEFAULT_INSTANCE
from .driver import Driver, Removal

# sizeof(sockaddr_un.sun_path) per host OS, as sys.platform spells it: a unix
# socket path of this many bytes or more cannot be bound.
UNIX_SOCKET_PATH_LIMITS = {"darwin": 104, "linux": 108}
# OpenSSH binds a master's socket at "<ControlPath>.<16 random characters>" and
# renames it into place (mux.c, muxserver_listen), so that is the path the limit
# applies to: the dot and the 16 characters come on top of the ControlPath.
SSH_MUX_TEMP_SUFFIX_LENGTH = len(".") + 16

# The unix sockets under a box's instance directory, each with the bytes its
# binder adds to the path. Not ssh.sock for the operator: that is Lima's
# hostagent's own persistent master, and a master yo does not own carries none
# of yo's forwards. The broker binds its two in place.
OPERATOR_CONTROL_SOCK = "yo-op.sock"
AGENT_CONTROL_SOCK = "ssh-agent.sock"
AWS_BROKER_DIR = "aws-broker"
AWS_BROKER_SOCK = AWS_BROKER_DIR + "/guest.sock"
AWS_BROKER_CTL_SOCK = AWS_BROKER_DIR + "/ctl.sock"
INSTANCE_SOCKETS = (
    (OPERATOR_CONTROL_SOCK, SSH_MUX_TEMP_SUFFIX_LENGTH),
    (AGENT_CONTROL_SOCK, SSH_MUX_TEMP_SUFFIX_LENGTH),
    (AWS_BROKER_SOCK, 0),
    (AWS_BROKER_CTL_SOCK, 0),
)


def hostname_for(name: str) -> str:
    """The guest hostname of the machine `name`.

    The default machine is `yolobox`, any other `yolobox-<name>`, lowercased
    because DNS labels are case-insensitive and a mesh VPN lowercases them. The
    result is not checked here: instance_name() refuses a machine whose
    hostname is no DNS label, before anything runs.
    """
    if name == DEFAULT_INSTANCE:
        return name
    return ("yolobox-" + name).lower()


@dataclass(frozen=True)
class Box:
    """The machine yo drives: a host driver and the machine's name.

    The machine comes from -m/--machine, and every per-machine name derives from
    these two values here, so one flag switches all of them. The driver says
    where its instances live.
    """

    driver: Driver
    name: str

    @property
    def vm_name(self) -> str:
        """What the driver calls the VM's libvirt domain, disk and cidata volumes."""
        return "yolobox-" + self.name

    @property
    def hostname(self) -> str:
        return hostname_for(self.name)

    @property
    def vm_host(self) -> str:
        # The ssh alias on both hosts: lima's name for its instance, which the
        # seed image also takes as its hostname from the cidata yo writes.
        return "lima-" + self.name

    @property
    def herdr_alias(self) -> str:
        return self.name

    @property
    def tilde_dir(self) -> str:
        return "~/%s/%s" % (self.driver.instances_dir, self.name)


# The machine every function that touches the VM's host side reads. Unset until
# main() chooses, so a code path that forgets to choose fails with an
# AttributeError (the module has no `box`), never with a guess.
box: Box


def set_box(driver: Driver, name: str) -> None:
    global box
    box = Box(driver, name)


def driver() -> Driver:
    """The host driver main() chose."""
    return box.driver


def yo_cmd(*words: str) -> str:
    """A `yo` command line for a printed remedy, naming the machine when it is
    not the default one."""
    if box.name == DEFAULT_INSTANCE:
        return "yo " + " ".join(words)
    return "yo -m %s %s" % (box.name, " ".join(words))


def recreate_chain(flags: Sequence[str] = ()) -> str:
    """The remedy for a box that must be made anew, to paste after the reason:
    `yo destroy --yes`, then `yo bootstrap FLAGS`, which creates it. It always
    says the disk is lost, so no caller can print it without the warning.
    Drivers build their recreate_command() with it."""
    return "recreate the box, which deletes everything on it: %s && %s" % (
        yo_cmd("destroy", "--yes"),
        yo_cmd("bootstrap", *flags),
    )


def hint(template: str) -> str:
    """A DriverHints text with this machine's names filled in."""
    return template.format(name=box.name, vm_name=box.vm_name, dir=box.tilde_dir)


def not_up_hint(what: str, parenthesized: bool = False) -> str:
    """`what` failed, so ask whether the VM is up, and name the command that tells."""
    status = yo_cmd("status")
    return "%s — is it up? %s" % (what, "(%s)" % status if parenthesized else status)


def home() -> str:
    # Normalised: HOME=/Users/x/ must equal a repo toplevel of /Users/x.
    return os.path.normpath(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)


def instance_dir_of(instances_dir: str, name: str) -> str:
    """The state directory of the machine `name` under a driver's instances root."""
    return os.path.join(home(), instances_dir, name)


def instance_dir() -> str:
    """This machine's state directory under the driver's instances root (both drivers)."""
    return instance_dir_of(driver().instances_dir, box.name)


def sockets_refusal(directory: str, platform: str) -> Optional[str]:
    """Why the instance directory `directory` cannot hold the box's unix
    sockets on the host OS `platform`, or None when every one fits. ssh would
    exit 255 on a master's, which reads as a box that cannot be reached, and
    the broker would fail its handshake."""
    limit = UNIX_SOCKET_PATH_LIMITS[platform]
    for name, suffix in INSTANCE_SOCKETS:
        path = os.path.join(directory, name)
        length = len(os.fsencode(path)) + suffix
        if length >= limit:
            with_suffix = (
                " plus the %d bytes of ssh's temporary socket name" % suffix
                if suffix
                else ""
            )
            return (
                "%s%s is %d bytes, and a unix socket path must be shorter than %d "
                "on this host: $HOME (%s) plus the machine name '%s' is too long "
                "for yo's sockets; pick a shorter -m name"
                % (
                    path,
                    with_suffix,
                    length,
                    limit,
                    home(),
                    os.path.basename(directory),
                )
            )
    return None


def ssh_cfg() -> str:
    return os.path.join(instance_dir(), "ssh.config")


def operator_control_path() -> str:
    return os.path.join(instance_dir(), OPERATOR_CONTROL_SOCK)


def agent_control_path() -> str:
    return os.path.join(instance_dir(), AGENT_CONTROL_SOCK)


def aws_broker_dir() -> str:
    """This box's AWS broker: its sockets, yo's lock on it and its log. One
    broker per box, since each watches its own box's keepalive process."""
    return os.path.join(instance_dir(), AWS_BROKER_DIR)


def aws_broker_sock() -> str:
    """The socket forwarded into the guest by the operator's ssh master."""
    return os.path.join(instance_dir(), AWS_BROKER_SOCK)


def aws_broker_ctl_sock() -> str:
    """The broker's host-only socket, for yo's /health. Never forwarded."""
    return os.path.join(instance_dir(), AWS_BROKER_CTL_SOCK)


def known_hosts_path() -> str:
    return os.path.join(instance_dir(), "known_hosts")


def state_dir() -> str:
    return os.path.join(home(), ".local/state/yolobox")


def tilde(path: str) -> str:
    """The path as an operator would type it: ~/... for one under $HOME."""
    prefix = home() + os.sep
    return "~/" + path[len(prefix) :] if path.startswith(prefix) else path


def tree_removal(path: str) -> Removal:
    def remove(held: HeldBox) -> None:
        proc.delete_tree(path)

    return Removal("delete directory %s" % tilde(path), remove)


class HeldBox:
    """Proof that this yo holds the box lock. Only
    box_lock() makes one, and every step that must run under the lock takes it
    as an argument: such a step called without the lock is a missing argument,
    and a step under the lock never takes it again. flock is not re-entrant
    across two descriptors of one process, so a second box_lock() inside the
    first would wait on itself forever."""

    def __init__(self, key: object) -> None:
        if key is not HELD_BOX_KEY:
            raise TypeError("only state.box_lock() makes a HeldBox")


HELD_BOX_KEY = object()
# Every command that takes the box lock: whichever may hold it, for a message.
BOX_LOCK_TAKERS = (
    ("up",),
    ("bootstrap",),
    ("disk-grow",),
    ("down",),
    ("destroy", "--yes"),
    ("status",),
)


def box_lock_holders() -> str:
    """The commands that may hold this box's lock, as a message names them."""
    commands = [yo_cmd(*words) for words in BOX_LOCK_TAKERS]
    return "%s or %s" % (", ".join(commands[:-1]), commands[-1])


def say_box_lock_wait() -> None:
    ui.err(
        "yo: another yo is acting on %s (%s holds its lock); waiting for it"
        % (box.name, box_lock_holders())
    )


# The boxes whose lock this process holds now, to refuse a nested box_lock().
held_boxes: Set[str] = set()


def box_lock_path() -> str:
    return os.path.join(state_dir(), "%s.box.lock" % box.name)


@contextlib.contextmanager
def box_lock(wait: bool) -> Iterator[HeldBox]:
    """Hold this box's exclusive host-side lock: the one chokepoint between two
    yo commands that could act on the box at once.

    A command that creates, starts, unlocks, stops, grows, rebuilds or
    destroys the box takes it once, before its first read of the box, holds it
    to its end, and hands the HeldBox down. `yo up`, `yo bootstrap` and `yo
    disk-grow` wait for it, saying so in one line when another yo holds it;
    `yo down` and `yo destroy` refuse instead (`wait` False raises LockBusy),
    since an up in progress may be sending the passphrase that starts the
    in-place encryption. The lock file is never deleted, `yo destroy` included.
    """
    name = box.name
    if name in held_boxes:
        raise RuntimeError(
            "box_lock(%s) taken again inside itself: it would wait on itself; "
            "pass the HeldBox down instead" % name
        )
    with common.held_flock(box_lock_path(), wait, say_box_lock_wait):
        held_boxes.add(name)
        try:
            yield HeldBox(HELD_BOX_KEY)
        finally:
            held_boxes.discard(name)
