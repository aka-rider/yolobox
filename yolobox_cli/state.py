"""The machine yo drives, the paths under the user's home that belong to it, and remedy commands."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import pwd
import shlex
import sys
from dataclasses import dataclass
from typing import ContextManager, Iterator, Optional

from . import proc
from .config import AGENT_USER, DEFAULT_INSTANCE, FILE_MODE_PRIVATE
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
        return "lima-" + self.name

    @property
    def herdr_alias(self) -> str:
        return self.name

    @property
    def tilde_dir(self) -> str:
        return "~/%s/%s" % (self.driver.instances_dir, self.name)

    @property
    def editor_ssh_block(self) -> str:
        # ssh keeps the first value it sees per keyword, so this block beats both
        # lima's Host block below it and a user's own "Host lima-yolobox /
        # ForwardAgent yes" included after it: the agent role never forwards the
        # Mac's agent.
        return (
            "Match host %s user %s\n  ControlPath %s/ssh-agent.sock\n"
            "  ForwardAgent no" % (self.vm_host, AGENT_USER, self.tilde_dir)
        )


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


def agent_control_path() -> str:
    return os.path.join(instance_dir(), "ssh-agent.sock")


def require_control_path_fits() -> None:
    """Raise a YoError naming the limit when the agent's ControlPath cannot be a unix
    socket. ssh would exit 255 on it, which reads as a box that cannot be reached."""
    path = agent_control_path()
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


def file_removal(path: str) -> Removal:
    return Removal(
        "delete file %s" % tilde(path), functools.partial(proc.delete_file, path)
    )


def tree_removal(path: str) -> Removal:
    return Removal(
        "delete directory %s" % tilde(path), functools.partial(proc.delete_tree, path)
    )


def flake_ref_file() -> str:
    return os.path.join(state_dir(), "%s.flake-ref" % box.name)


def remembered_flake_ref() -> Optional[str]:
    """The ref the last bootstrap built this machine from, if it ever did."""
    text = proc.read_text_file(flake_ref_file(), errors="surrogateescape")
    ref = text.strip() if text is not None else ""
    return ref or None


def destroy_then_bootstrap_command() -> str:
    """The command line that deletes this machine and bootstraps it again from
    the ref it was built from: destroy deletes the file that remembers the ref,
    so the bootstrap names it here."""
    ref = remembered_flake_ref()
    version = [] if ref is None else ["--version", shlex.quote(ref)]
    return "%s && %s" % (
        yo_cmd("destroy", "--yes"),
        yo_cmd("bootstrap", *version),
    )


class LockBusy(Exception):
    """Another process holds the lock, and the caller asked not to wait."""


@contextlib.contextmanager
def held_flock(path: str, wait: bool) -> Iterator[None]:
    """Hold an exclusive flock on `path`, created private if it is missing.

    With `wait` False a held lock raises LockBusy instead of blocking.

    The lock file is never deleted. A process blocked in `flock` holds the old
    inode: with the file unlinked under it, the next one would create and lock
    a fresh file, and the two would hold "the" lock at once. Like
    `instances/.port.lock`, it is a few bytes of host state.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE_PRIVATE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy()
        yield
    finally:
        os.close(fd)  # closing the descriptor drops the lock


def host_state_lock(name: str, wait: bool) -> ContextManager[None]:
    """`held_flock` on the file `name` in the state directory, which it creates."""
    proc.private_dir(state_dir())
    return held_flock(os.path.join(state_dir(), name), wait)


def unlock_lock(wait: bool) -> ContextManager[None]:
    """Hold this box's exclusive host-side lock on its initrd's passphrase ask.

    The one chokepoint between the yo that answers the ask and the yo that
    powers the box off: whoever sends the passphrase holds it while sending, and
    `yo down` holds it from its probe to the power-off, so the answer can never
    land between the two. `yo destroy` holds it for its whole run. With `wait`
    False a held lock raises LockBusy instead of blocking, which those two
    turn into a refusal. The lock file is never deleted, `yo destroy` included.
    """
    return host_state_lock("%s.unlock.lock" % box.name, wait)
