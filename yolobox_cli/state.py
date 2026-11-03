"""The machine yo drives, the paths under the user's home that belong to it, and remedy commands."""

from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass

from .config import AGENT_USER, DEFAULT_INSTANCE
from .driver import Driver


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

    @property
    def include_line(self) -> str:
        return "Include %s/ssh.config" % self.tilde_dir

    @property
    def include_re(self) -> re.Pattern[str]:
        return re.compile(
            r"^\s*Include\s+.*%s/%s/ssh\.config"
            % (re.escape(self.driver.instances_dir), re.escape(self.name)),
            re.IGNORECASE,
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


def known_hosts_path() -> str:
    return os.path.join(instance_dir(), "known_hosts")


def state_dir() -> str:
    return os.path.join(home(), ".local/state/yolobox")
