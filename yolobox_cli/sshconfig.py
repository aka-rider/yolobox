"""The ssh config file yo writes per machine and the Include line in the user's own config."""

from __future__ import annotations

import os
import stat
import sys
from typing import List

from . import common, proc, state, ui
from .config import AGENT_USER, FILE_MODE_PRIVATE, LOOPBACK, SSH_CONFIG_INCLUDE_LINE
from .driver import SshFacts
from .ui import YoError


# The ssh options of every probe of the initrd's sshd.
SSH_CONNECT_TIMEOUT_SECONDS = 5
SSH_ALIVE_INTERVAL_SECONDS = 15
STAGE2_CONNECT_TIMEOUT_SECONDS = 30
STAGE2_ALIVE_COUNT_MAX = 8


def ssh_opt(key: str, value: str) -> List[str]:
    """`-o key=value` for ssh's command line, the one place a value is quoted.

    ssh reads an -o argument as a config line, so a value with whitespace (a
    socket path under a $HOME with a space) is cut at the first blank unless
    it is double-quoted, and ssh then fails with exit 255, which reads as a
    box that cannot be reached. A value holding a double quote has no spelling
    at all, and is refused rather than mangled."""
    if '"' in value:
        raise YoError(
            "ssh's %s cannot be %r: a value with a double quote cannot be passed "
            "to ssh" % (key, value)
        )
    if any(char.isspace() for char in value):
        value = '"%s"' % value
    return ["-o", "%s=%s" % (key, value)]


def quoted(value: str) -> str:
    """A path as an ssh config line takes it: double-quoted (a blank would end it)."""
    if '"' in value:
        raise YoError(
            "%r cannot be written into ssh.config: it holds a double quote" % value
        )
    return '"%s"' % value


def host_block(host: str, facts: SshFacts, user: str, control_path: str) -> List[str]:
    """One Host block: every keyword set, so nothing a user's own ~/.ssh/config
    says after the Include line changes how yo's roles connect."""
    return [
        "Host %s" % host,
        "  Hostname %s" % LOOPBACK,
        "  Port %d" % facts.port,
        "  User %s" % user,
        "  IdentityFile %s" % quoted(facts.identity_file),
        "  IdentityAgent %s" % quoted(facts.identity_agent),
        "  IdentitiesOnly yes",
        "  BatchMode yes",
        "  PreferredAuthentications publickey",
        "  PasswordAuthentication no",
        "  KbdInteractiveAuthentication no",
        "  GSSAPIAuthentication no",
        "  AddKeysToAgent no",
        # Only `yo ssh` forwards an agent, by its own command line.
        "  ForwardAgent no",
        "  Compression no",
        "  ControlMaster auto",
        "  ControlPath %s" % quoted(control_path),
        "  ControlPersist yes",
        # The pin: accept-new learns the key on first use when
        # ensure_guest_known_hosts has not stored one yet. It is keyed on the
        # machine's name, never on the port, which can change: a pin ssh
        # cannot find is a key learned anew from whatever answers the new port.
        # Both blocks name the one alias, so one pin serves both.
        "  HostKeyAlias %s" % state.box.vm_host,
        "  UserKnownHostsFile %s" % quoted(state.known_hosts_path()),
        "  StrictHostKeyChecking accept-new",
        "  NoHostAuthenticationForLocalhost no",
        # A guest that stops answering (paused, frozen) ends the connection
        # after interval times count, and a busy guest has that long to answer.
        "  ConnectTimeout %d" % STAGE2_CONNECT_TIMEOUT_SECONDS,
        "  ServerAliveInterval %d" % SSH_ALIVE_INTERVAL_SECONDS,
        "  ServerAliveCountMax %d" % STAGE2_ALIVE_COUNT_MAX,
    ]


def ssh_config_text(facts: SshFacts) -> str:
    """The instance's ssh.config: the operator's block under the ssh alias, the
    agent role's own master, and herdr's alias as the agent."""
    agent_block = [
        # ssh keeps the first value it sees per keyword, so this block beats
        # the Host block below it: the agent role has its own master.
        "Match host %s user %s" % (state.box.vm_host, AGENT_USER),
        "  ControlPath %s" % quoted(state.agent_control_path()),
    ]
    # Never share yo's own multiplexed master: OpenSSH's mux client does not
    # re-check a new connection's login user against the master's, so herdr
    # would silently ride an operator session.
    herdr_block = host_block(state.box.herdr_alias, facts, AGENT_USER, "none")
    operator_block = host_block(
        state.box.vm_host,
        facts,
        facts.user,
        state.operator_control_path(),
    )
    return "\n".join(agent_block + [""] + operator_block + [""] + herdr_block) + "\n"


def write_ssh_config() -> None:
    """Write this machine's ssh.config from the driver's facts, when it differs.

    The one writer: on a Mac lima regenerates the file in its own shape at
    every start, and this puts yo's back after the start."""
    wanted = ssh_config_text(state.driver().ssh_facts())
    current = proc.read_text_file(state.ssh_cfg(), errors="surrogateescape")
    if current != wanted:
        proc.atomic_write(state.ssh_cfg(), wanted, FILE_MODE_PRIVATE)


def known_hosts_lines(pubkeys: str) -> List[str]:
    """The pin file's lines for the keys `pubkeys` holds, under the HostKeyAlias."""
    lines = []
    for raw in pubkeys.splitlines():
        parts = raw.split(None, 2)
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        keytype, key = parts[0], parts[1]
        lines.append("%s %s %s" % (state.box.vm_host, keytype, key))
    return lines


# The one opt-out from the pin written into ssh.config. A command-line -o
# outranks the file, so these beat the pin's three keywords.
FIRST_CONTACT_OPTS = [
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "NoHostAuthenticationForLocalhost=yes",
]


def ssh_config_reaches_box() -> bool:
    """Whether a bare `ssh <vm_host>` uses this machine's ssh.config: ssh itself
    reads ~/.ssh/config and says which known_hosts and which name in it it
    would pin against.

    Asking ssh, not grepping, gets right what a grep cannot: a commented line,
    a glob, several paths on one Include, and an Include after a Host or Match
    line, which applies only to that block. False when the file is missing; an
    ssh that refuses the file (a bad keyword, loose permissions) is an error."""
    config_path = os.path.join(state.home(), ".ssh", "config")
    try:
        info = os.stat(config_path)
    except FileNotFoundError:
        return False
    # A bare ssh refuses a ~/.ssh/config that is group- or world-writable or
    # owned by another account, but `ssh -F` checks no permissions, so the
    # rule is ssh's own (secure_permissions in OpenSSH's misc.c), applied here.
    if info.st_uid not in (0, os.getuid()):
        raise YoError(
            "ssh cannot read %s: bad owner (uid %d, not yours) — a bare ssh refuses "
            "it; fix it with: sudo chown %d %s"
            % (config_path, info.st_uid, os.getuid(), config_path)
        )
    if info.st_mode & 0o022:
        raise YoError(
            "ssh cannot read %s: bad permissions (mode %04o, writable by others) — "
            "a bare ssh refuses it; fix it with: chmod go-w %s"
            % (config_path, stat.S_IMODE(info.st_mode), config_path)
        )
    completed = proc.run(["ssh", "-F", config_path, "-G", state.box.vm_host])
    if completed.returncode != 0:
        raise YoError(
            "ssh cannot read %s: %s"
            % (
                config_path,
                completed.stderr.strip() or "exit %d" % completed.returncode,
            )
        )
    effective = completed.stdout.splitlines()
    return (
        "userknownhostsfile %s" % state.known_hosts_path() in effective
        and "hostkeyalias %s" % state.box.vm_host in effective
    )


def add_ssh_config_include() -> None:
    """Put the include line on top of ~/.ssh/config, which every box on this
    host shares: under the host's lock on that file, read again after taking
    it, so two yo commands neither add it twice nor drop an edit."""
    with common.held_flock(os.path.join(state.state_dir(), "ssh-config.lock"), True):
        if not ssh_config_reaches_box():
            prepend_ssh_config_include()


def prepend_ssh_config_include() -> None:
    ssh_dir = os.path.join(state.home(), ".ssh")
    if not os.path.isdir(ssh_dir):
        proc.private_dir(ssh_dir)
    # ~/.ssh/config may be a symlink into a version-controlled directory: write through
    # it, keep the mode the user chose, and never rewrite undecodable bytes.
    config_path = os.path.realpath(os.path.join(ssh_dir, "config"))
    try:
        with open(config_path, "r", errors="surrogateescape") as handle:
            current = handle.read()
        mode = stat.S_IMODE(os.stat(config_path).st_mode)
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(config_path)):
            raise YoError(
                '~/.ssh/config is a symlink to "%s", whose directory does not exist; '
                "fix or remove the link, then run this again" % config_path
            )
        current = ""
        mode = FILE_MODE_PRIVATE
    except OSError as failure:
        raise YoError("cannot read %s: %s" % (config_path, ui.os_error_reason(failure)))
    proc.atomic_write(config_path, SSH_CONFIG_INCLUDE_LINE + "\n" + current, mode)


def ensure_ssh_config_include(refuse: bool) -> bool:
    """True when ~/.ssh/config reaches the VM's file; False when declined and not refusing."""
    if ssh_config_reaches_box():
        return True

    if sys.stdin.isatty():
        ui.err(
            "yo: ~/.ssh/config does not reach the VM's ssh config, so Zed, VS Code, herdr"
        )
        ui.err(
            "    and a bare `ssh %s` cannot resolve the host. yo can add one line, at the"
            % state.box.vm_host
        )
        ui.err(
            "    top, that serves every machine on this host (an Include below a Host or"
        )
        ui.err("    Match line applies only to that block):")
        ui.err("        %s" % SSH_CONFIG_INCLUDE_LINE)
        try:
            answer = input("    Add it to ~/.ssh/config now? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("y", "yes"):
            add_ssh_config_include()
            ui.out("yo: added '%s' to ~/.ssh/config" % SSH_CONFIG_INCLUDE_LINE)
            return True

    ui.err(
        "yo: add this line to the top of ~/.ssh/config, above any Host or Match line, so"
        " ssh %s, Zed, VS Code and herdr can find the VM:" % state.box.vm_host
    )
    ui.err("    %s" % SSH_CONFIG_INCLUDE_LINE)
    if refuse:
        raise YoError(None, code=1)
    return False
