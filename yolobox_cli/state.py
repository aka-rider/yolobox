"""The machine yo drives, the paths under the user's home that belong to it, and remedy commands."""

from __future__ import annotations

import functools
import os
import pwd
import sys
from dataclasses import dataclass
from typing import ContextManager, Sequence

from . import common, proc
from .config import DEFAULT_INSTANCE
from .driver import Driver, Removal
from .ui import YoError

# sizeof(sockaddr_un.sun_path): a unix socket path of this many bytes or more cannot be bound.
UNIX_SOCKET_PATH_LIMIT = 104 if sys.platform == "darwin" else 108
# OpenSSH binds a master's socket at "<ControlPath>.<16 random characters>" and
# renames it into place (mux.c, muxserver_listen), so that is the path the limit
# applies to: the dot and the 16 characters come on top of the ControlPath.
SSH_MUX_TEMP_SUFFIX_LENGTH = len(".") + 16


def hostname_for(name: str) -> str:
    """The guest hostname, and tailnet node name, of the machine `name`.

    The default machine is `yolobox`, any other `yolobox-<name>`, lowercased
    because DNS labels are case-insensitive and tailscale lowercases them. The
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


def recreate_chain(*between: str, flags: Sequence[str] = ()) -> str:
    """The remedy for a box that must be made anew, to paste: `yo destroy --yes`,
    the commands a creation needs first (`between`), then `yo bootstrap FLAGS`.
    Drivers build their recreate_command() with it."""
    return " && ".join(
        [yo_cmd("destroy", "--yes"), *between, yo_cmd("bootstrap", *flags)]
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


def instance_dir() -> str:
    """This machine's state directory under the driver's instances root (both drivers)."""
    return os.path.join(home(), driver().instances_dir, box.name)


def ssh_cfg() -> str:
    return os.path.join(instance_dir(), "ssh.config")


def operator_control_path() -> str:
    return os.path.join(instance_dir(), "ssh.sock")


def agent_control_path() -> str:
    return os.path.join(instance_dir(), "ssh-agent.sock")


def require_control_path_fits() -> None:
    """Raise a YoError naming the limit when the longest ControlPath yo writes cannot
    be a unix socket. ssh would exit 255 on it, which reads as a box that cannot be
    reached."""
    path = max((operator_control_path(), agent_control_path()), key=len)
    length = len(os.fsencode(path)) + SSH_MUX_TEMP_SUFFIX_LENGTH
    if length >= UNIX_SOCKET_PATH_LIMIT:
        raise YoError(
            "%s plus the %d bytes of ssh's temporary socket name is %d bytes, and a "
            "unix socket path must be shorter than %d: "
            "$HOME (%s) is too long for yo's ssh master socket"
            % (
                path,
                SSH_MUX_TEMP_SUFFIX_LENGTH,
                length,
                UNIX_SOCKET_PATH_LIMIT,
                home(),
            )
        )


def known_hosts_path() -> str:
    return os.path.join(instance_dir(), "known_hosts")


def state_dir() -> str:
    return os.path.join(home(), ".local/state/yolobox")


def tilde(path: str) -> str:
    """The path as an operator would type it: ~/... for one under $HOME."""
    prefix = home() + os.sep
    return "~/" + path[len(prefix) :] if path.startswith(prefix) else path


def tree_removal(path: str) -> Removal:
    return Removal(
        "delete directory %s" % tilde(path), functools.partial(proc.delete_tree, path)
    )


def unlock_lock(wait: bool) -> ContextManager[None]:
    """Hold this box's exclusive host-side lock on its initrd's passphrase ask.

    The one chokepoint between the yo that answers the ask and the yo that
    powers the box off: whoever sends the passphrase holds it while sending, and
    `yo down` holds it from its probe to the power-off, so the answer can never
    land between the two. `yo destroy` holds it for its whole run. With `wait`
    False a held lock raises LockBusy instead of blocking, which those two
    turn into a refusal. The lock file is never deleted, `yo destroy` included.
    """
    return common.held_flock(
        os.path.join(state_dir(), "%s.unlock.lock" % box.name), wait
    )
