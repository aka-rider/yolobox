"""The ssh config files yo writes, the Include line in the user's own config and the initrd endpoint."""

from __future__ import annotations

import os
import stat
import sys
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import proc, state, ui
from .config import AGENT_USER, FILE_MODE_PRIVATE, SSH_CONFIG_INCLUDE_LINE
from .ui import YoError


# The ssh options of every probe of the initrd's sshd.
SSH_CONNECT_TIMEOUT_SECONDS = 5
SSH_ALIVE_INTERVAL_SECONDS = 15
# Every connection to the box's two names in ssh.config (yo's roles, the
# master, nixos-rebuild, editors through lima-NAME; herdr through its alias
# NAME): a guest that stops answering (paused, frozen) ends the connection
# after interval times count, 2 minutes, and a busy guest has that long to
# answer one keepalive. Neither lima's block nor libvirt_ssh_config sets them.
STAGE2_CONNECT_TIMEOUT_SECONDS = 30
STAGE2_ALIVE_COUNT_MAX = 8

AGENT_SSH_CONFIG_MARKER = (
    "# yolobox: re-applied by yo; lima rewrites this file on every start"
)


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


class SshHostBlock(NamedTuple):
    """What yo reads of an instance's Host block in ssh.config."""

    hostname: str
    port: str
    # Every IdentityFile, in file order: ssh offers each in turn.
    identity_files: Tuple[str, ...]
    # Set when the instance's block names one (the libvirt driver's names the
    # 1Password socket, because its IdentityFile is only a public key).
    identity_agent: Optional[str]


def parse_ssh_host_block(text: str, host: str) -> Optional[SshHostBlock]:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "Host %s" % host:
            start = i + 1
            break
    if start is None:
        return None
    # ssh keeps the first value of Hostname, Port and IdentityAgent and tries
    # every IdentityFile, so this does the same.
    first: Dict[str, str] = {}
    identity_files: List[str] = []
    for line in lines[start:]:
        if line and not line[0].isspace():
            break
        key, _, value = line.strip().partition(" ")
        value = value.strip().strip('"')
        if key == "IdentityFile":
            if value:
                identity_files.append(value)
        elif key in ("Hostname", "Port", "IdentityAgent"):
            first.setdefault(key, value)
    if not (first.get("Hostname") and first.get("Port") and identity_files):
        return None
    return SshHostBlock(
        first["Hostname"],
        first["Port"],
        tuple(identity_files),
        first.get("IdentityAgent") or None,
    )


def herdr_alias_block(host_block: SshHostBlock) -> str:
    identity_agent = (
        ['  IdentityAgent "%s"' % host_block.identity_agent]
        if host_block.identity_agent is not None
        else []
    )
    return "\n".join(
        [
            "Host %s" % state.box.herdr_alias,
            "  User %s" % AGENT_USER,
            # Never share yo's own multiplexed master: OpenSSH's mux client
            # does not re-check a new connection's login user against the
            # master's, so herdr would silently ride an operator session.
            "  ControlPath none",
            "  ForwardAgent no",
            # herdr reaches this alias through ~/.ssh/config, where a later
            # `Host *` may switch loopback verification off; first value wins.
            "  NoHostAuthenticationForLocalhost no",
            "  Hostname %s" % host_block.hostname,
            "  Port %s" % host_block.port,
        ]
        + ['  IdentityFile "%s"' % identity for identity in host_block.identity_files]
        + [
            '  UserKnownHostsFile "%s"' % state.known_hosts_path(),
            "  IdentitiesOnly yes",
        ]
        + identity_agent
    )


def lima_part_of(current: str) -> str:
    if not current.startswith(AGENT_SSH_CONFIG_MARKER):
        return current
    _, blank_line, rest = current.partition("\n\n")
    if blank_line:
        return rest
    # Nothing says where our block ends, so keep everything after the marker
    # line: a duplicate owned block is harmless (first value wins), a lost
    # Host block is not.
    return current.partition("\n")[2]


def host_key_pin_block() -> str:
    # Lima's own Host block says StrictHostKeyChecking no, UserKnownHostsFile
    # /dev/null and NoHostAuthenticationForLocalhost yes, and the last one makes
    # ssh skip host-key verification for 127.0.0.1 whatever else is set (checked
    # against a throwaway sshd: a changed key still connected). ssh keeps the
    # first value per keyword, so this block, written above lima's, is the one
    # place that pins the key for every connection that reads this file: every
    # yo role, `yo code` and `yo zed`. Accept-new learns the
    # key on first use when ensure_guest_known_hosts has not stored one yet.
    return "\n".join(
        [
            "Match host %s" % state.box.vm_host,
            '  UserKnownHostsFile "%s"' % state.known_hosts_path(),
            "  StrictHostKeyChecking accept-new",
            "  NoHostAuthenticationForLocalhost no",
            "  ForwardAgent no",
        ]
    )


def connection_bounds_block() -> str:
    # Above the driver's Host block and the herdr alias, so these win for both
    # names: ssh keeps the first value.
    return "\n".join(
        [
            "Match host %s,%s" % (state.box.vm_host, state.box.herdr_alias),
            "  ConnectTimeout %d" % STAGE2_CONNECT_TIMEOUT_SECONDS,
            "  ServerAliveInterval %d" % SSH_ALIVE_INTERVAL_SECONDS,
            "  ServerAliveCountMax %d" % STAGE2_ALIVE_COUNT_MAX,
        ]
    )


def ensure_agent_ssh_config() -> None:
    state.require_control_path_fits()
    path = state.ssh_cfg()
    current = proc.read_text_file(path, errors="surrogateescape")
    if current is None:
        return

    rest = lima_part_of(current)

    owned = [
        AGENT_SSH_CONFIG_MARKER,
        state.box.editor_ssh_block,
        host_key_pin_block(),
        connection_bounds_block(),
    ]
    host_block = parse_ssh_host_block(rest, state.box.vm_host)
    if host_block is None:
        ui.err(
            "yo: %s has no parseable 'Host %s' block (Hostname/Port/IdentityFile) — "
            "skipping the %r ssh alias herdr needs; run %s to let lima regenerate it"
            % (path, state.box.vm_host, state.box.herdr_alias, state.yo_cmd("up"))
        )
    else:
        owned.append(herdr_alias_block(host_block))

    new_content = "\n".join(owned) + "\n\n" + rest
    if new_content == current:
        return
    proc.atomic_write(path, new_content, FILE_MODE_PRIVATE)


def known_hosts_lines(host_block: SshHostBlock, pubkeys: str) -> List[str]:
    host_port = "[%s]:%s" % (host_block.hostname, host_block.port)
    lines = []
    for raw in pubkeys.splitlines():
        parts = raw.split(None, 2)
        if len(parts) < 2:
            continue
        keytype, key = parts[0], parts[1]
        lines.append("%s %s %s" % (host_port, keytype, key))
    return lines


# The one opt-out from the pin host_key_pin_block writes into ssh.config. A
# command-line -o outranks the file, so these beat the pin's three keywords.
FIRST_CONTACT_OPTS = [
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "NoHostAuthenticationForLocalhost=yes",
]


# Where ssh.config may send the box's port: this host, by any of its spellings.
LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost", "::1")


class InitrdEndpoint(NamedTuple):
    """Where, and as whom, the pre-unlock probes reach the initrd's sshd."""

    hostname: str
    port: int
    identities: Tuple[str, ...]


def initrd_endpoint() -> InitrdEndpoint:
    # No file: a first start, lima has not written it yet, and the defaults below
    # are what it will write.
    text = proc.read_text_file(state.ssh_cfg(), errors="surrogateescape")
    host_block = (
        parse_ssh_host_block(text, state.box.vm_host) if text is not None else None
    )
    if not host_block:
        return InitrdEndpoint(
            "127.0.0.1",
            state.driver().ssh_port(),
            (os.path.join(state.home(), state.driver().initrd_identity),),
        )
    if not proc.is_ascii_number(host_block.port):
        raise YoError(
            "%s names the port %r, not a number" % (state.ssh_cfg(), host_block.port)
        )
    if host_block.hostname not in LOOPBACK_HOSTNAMES:
        # Everything that trusts the unpinned channel assumes the port is on
        # this host: libvirt's owner check reads the local listener, and Lima's
        # mirror is loopback-only. A box reached elsewhere would get the
        # passphrase with neither check behind it.
        raise YoError(
            "%s names the host %r for %s, which is not a loopback address (%s): "
            "the disk passphrase goes to the box's forwarded port on this host only"
            % (
                state.ssh_cfg(),
                host_block.hostname,
                state.box.vm_host,
                ", ".join(LOOPBACK_HOSTNAMES),
            )
        )
    return InitrdEndpoint(
        host_block.hostname, int(host_block.port), host_block.identity_files
    )


def initrd_ssh_argv(
    remote: str, endpoint: InitrdEndpoint, user: str = "root"
) -> List[str]:
    # Before the disk is unlocked only the initrd's sshd answers, with a host key
    # it generated this boot. Nothing is pinned, so whoever holds the loopback
    # port is trusted with the answer: on macOS that is lima's hostagent, running
    # as the host user; on Linux it is passt, running as the yolobox-vm account,
    # and state.driver().verify_initrd_listener() checks the owner before every
    # passphrase is sent. And no pinned role may be used here: accept-new would
    # write the initrd's key into known_hosts, and every stage-2 connection after
    # it would be refused. The caller reads the endpoint once, so the listener
    # it checked is the one this dials.
    hostname, port, identities = endpoint
    return (
        [
            "ssh",
            "-F",
            "/dev/null",
        ]
        + [arg for identity in identities for arg in ("-i", identity)]
        + ssh_opt("IdentitiesOnly", "yes")
        + ssh_opt("IdentityAgent", state.driver().initrd_identity_agent())
        + ssh_opt("User", user)
        + ssh_opt("Port", str(port))
        + ssh_opt("ControlPath", "none")
        + ssh_opt("BatchMode", "yes")
        + ssh_opt("ConnectTimeout", str(SSH_CONNECT_TIMEOUT_SECONDS))
        + ssh_opt("ServerAliveInterval", str(SSH_ALIVE_INTERVAL_SECONDS))
        + ssh_opt("LogLevel", "ERROR")
        + FIRST_CONTACT_OPTS
        + [hostname, remote]
    )


def ssh_config_reaches_box() -> bool:
    """Whether a bare `ssh <vm_host>` uses this machine's ssh.config: ssh itself
    reads ~/.ssh/config and says which known_hosts it would pin against.

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
    wanted = "userknownhostsfile %s" % state.known_hosts_path()
    return wanted in completed.stdout.splitlines()


def add_ssh_config_include() -> None:
    ssh_dir = os.path.join(state.home(), ".ssh")
    if not os.path.isdir(ssh_dir):
        os.mkdir(ssh_dir, 0o700)
        os.chmod(ssh_dir, 0o700)
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
