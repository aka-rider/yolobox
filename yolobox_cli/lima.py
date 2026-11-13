"""The Lima driver: the instance, its config gaps and its lifecycle."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import aws, driver, proc, release, state, ui, unlock
from .config import (
    DEFAULT_INSTANCE,
    GIB,
    KIB,
    LOOPBACK,
    MAX_PORT,
    OPERATOR_GUEST_UID,
    SCRIPT_DIR,
    SSH_LOCAL_PORT,
    USAGE_EXIT,
)
from .driver import (
    DriverHints,
    NewBox,
    Removal,
    Size,
    SshFacts,
    StopRefused,
    VmState,
    VmStatus,
)
from .ui import RunTimeout, YoError


# LimaDriver.config_gaps() entries: a setting on the instance that departs
# from lima/yolobox.yaml.
GAP_MOUNTS = "mounts"
GAP_FORWARD_AGENT = "ssh.forwardAgent"
GAP_SSH_OVER_VSOCK = "ssh.overVsock"
GAP_PROPAGATE_PROXY_ENV = "propagateProxyEnv"
GAP_LOAD_DOT_SSH_PUB_KEYS = "ssh.loadDotSSHPubKeys"
GAP_UDP_FORWARDS = "portForwards.udp"
GAP_TCP_FORWARDS = "portForwards.tcp"
GAP_REVERSE_SOCKETS = "portForwards.guestSocket"
GAP_OPERATOR_UID = "user.uid"
GAP_VM_TYPE = "vmType"

# The guest TCP ports lima mirrors onto the host's 127.0.0.1: unprivileged and
# below the macOS ephemeral range (49152). A Linux host mirrors nothing: it has no
# fixed range to give away, so `yo port` hands out host ports from
# host_port_window(), the ports above the kernel's ephemeral range.
MIRRORED_TCP_PORTS = [1024, 49151]

# The lowest port a non-default instance may use for ssh.localPort: above every
# mirrored guest port. A root login, the passphrase and the first host-key
# fetch go to that port without checking a host key, so a port inside the
# mirror could be answered by a listener of another running box.
MIN_SSH_LOCAL_PORT = MIRRORED_TCP_PORTS[1] + 1

# Fixed OAuth callback ports inside MIRRORED_TCP_PORTS that are never mirrored:
# Codex CLI 1455-1457, Databricks 8020, gcloud 8085, Firebase 9005, kubelogin 18000.
# (kubelogin's 8000 stays mirrored on purpose: it is also the Django and
# http.server default.) Claude Code needs no entry: its login listens on an
# ephemeral port, above the mirror.
OAUTH_CALLBACK_PORT_RANGES = [
    [1455, 1457],
    [8020, 8020],
    [8085, 8085],
    [9005, 9005],
    [18000, 18000],
]

# The order matters: lima takes the first matching rule, so the deny-list comes
# before the range and the catch-alls come last.
LIMA_PORT_FORWARDS = [
    *[
        {
            "proto": "tcp",
            "guestIP": "0.0.0.0",
            "guestIPMustBeZero": False,
            "guestPortRange": port_range,
            "ignore": True,
        }
        for port_range in OAUTH_CALLBACK_PORT_RANGES
    ],
    {"proto": "tcp", "guestPortRange": MIRRORED_TCP_PORTS},
    {"proto": "udp", "guestIP": "0.0.0.0", "guestIPMustBeZero": False, "ignore": True},
    {"proto": "tcp", "guestIP": "0.0.0.0", "guestIPMustBeZero": False, "ignore": True},
]

RESTATE_PORT_FORWARDS = ".portForwards = %s" % json.dumps(LIMA_PORT_FORWARDS)

# Per gap: what the instance does wrong, and the `limactl edit` expression
# that undoes it while the instance is stopped. None: lima reads the setting
# only when it creates an instance, so the instance must be recreated.
LIMA_GAPS: Dict[str, Tuple[str, Optional[str]]] = {
    GAP_MOUNTS: ("mounts host directories into the VM", ".mounts = []"),
    GAP_FORWARD_AGENT: (
        "forwards the host's ssh agent to every lima ssh session",
        ".ssh.forwardAgent = false",
    ),
    GAP_SSH_OVER_VSOCK: (
        "lets lima ssh over vsock, where the initrd cannot answer and the "
        "operator is locked out",
        ".ssh.overVsock = false",
    ),
    GAP_PROPAGATE_PROXY_ENV: (
        "copies the host's proxy variables into the VM",
        ".propagateProxyEnv = false",
    ),
    GAP_LOAD_DOT_SSH_PUB_KEYS: (
        "adds the host's ~/.ssh public keys to the VM's authorized_keys",
        ".ssh.loadDotSSHPubKeys = false",
    ),
    GAP_UDP_FORWARDS: (
        "mirrors guest UDP listeners onto the host",
        RESTATE_PORT_FORWARDS,
    ),
    GAP_TCP_FORWARDS: (
        "mirrors other guest TCP ports than lima/yolobox.yaml does",
        RESTATE_PORT_FORWARDS,
    ),
    GAP_REVERSE_SOCKETS: (
        "reverses host sockets into the guest, which yo's own ssh master forwards",
        RESTATE_PORT_FORWARDS,
    ),
    GAP_OPERATOR_UID: (
        "gives the operator another uid than %d" % OPERATOR_GUEST_UID,
        None,
    ),
    GAP_VM_TYPE: ("is not a vz instance, so it has no rosetta share", None),
}

# The gaps start() refuses to start with. Every other gap is a warning: they
# leak but lock nobody out and break no promise the box makes. Over vsock, the
# initrd that unlocks the disk cannot answer, and the guest's sshd admits the
# operator only from 192.168.5.2, which a vsock peer, having no IP address,
# never is. A mount hands the agent a host directory, which is the one thing
# the box promises never to do.
LIMA_FATAL_GAPS = frozenset([GAP_SSH_OVER_VSOCK, GAP_MOUNTS])

# The vmType lima/yolobox.yaml pins; the rosetta share exists under it only.
LIMA_VM_TYPE = "vz"

LIMA_STOP_TIMEOUT = 300
# What `limactl start` waits for lima's ssh before it gives up: the seed's first
# boot encrypts the root in place first, which can take an hour on a big disk.
LIMA_START_TIMEOUT = "4h"

# limactl's status for an instance it cannot load: a lima.yaml it refuses, or a
# hostagent pid file left behind.
BROKEN = "Broken"
# What limactl reports while `limactl start` is still creating, downloading or
# booting: a start waits through these and through an unlisted instance.
PRE_BOOT_STATUSES = ("Installing", "Starting", "Stopped")
# lima's own key file, which logs in as the operator and, in the initrd, as root.
LIMA_USER_KEY = ".lima/_config/user"


class BrokenInstance(YoError):
    """limactl reports the instance as Broken; the message quotes what it says.

    Every reader of the instance's settings raises it, since a listing that
    lima could not load has no settings to judge. vm_status() alone turns it
    back into the status, for the callers that decide by the status and say
    where to look themselves.
    """


# st_blocks counts blocks of this size whatever the filesystem's own is (POSIX).
STAT_BLOCK_BYTES = 512


# The host drivers. Everything from here to the libvirt block is the macOS side:
# what limactl reports and does, behind LimaDriver. Callers outside this block
# reach it only through state.driver().
@dataclass(frozen=True)
class LimaInfo:
    host_agent_pid: Optional[int]
    port_forwards: List[dict]
    mounts: object  # as limactl reports it: None when the instance has none
    forward_agent: bool
    status: object  # as limactl reports it: vm_status takes only a word
    over_vsock: Optional[bool]
    propagate_proxy_env: Optional[bool]
    load_dot_ssh_pub_keys: Optional[bool]
    user_uid: object  # the operator's uid as lima created it
    ssh_local_port: object  # as limactl reports it: 0 means lima picks per start
    vm_type: object  # as limactl reports it


def limactl(*args: str) -> List[str]:
    """The argv of one limactl command: the one place that builds it.

    Warnings and errors only: lima's info lines narrate every step of a start,
    stop or delete that yo narrates itself.
    """
    return ["limactl", "--log-level=warn", *args]


def lima_instance_exists() -> bool:
    """Whether limactl knows this machine's name: the one place that decides it.

    A failing `limactl list -q` is an error, never "no instance": a box whose
    limactl is broken would otherwise look like a new one to create.
    """
    argv = limactl("list", "-q")
    completed = proc.checked(proc.run(argv), shlex.join(argv))
    return state.box.name in completed.stdout.splitlines()


def lima_instance_info() -> Optional[LimaInfo]:
    """What limactl reports of this machine; None when there is no such instance.

    Any failure to read an instance that exists is an error, never a missing
    instance: a box whose limactl is broken would otherwise look like one with
    no gaps.
    """
    if not lima_instance_exists():
        return None
    argv = limactl("list", state.box.name, "--json")
    completed = proc.checked(proc.run(argv), shlex.join(argv))
    try:
        data = json.loads(completed.stdout.strip())
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise YoError(
            "%s did not print one JSON object: %r"
            % (shlex.join(argv), completed.stdout.strip()[:200])
        )
    status = data.get("status")
    if status == BROKEN:
        raise BrokenInstance(broken_instance_message(argv, data))
    if "config" not in data:
        raise YoError(
            "%s printed no config for %s" % (shlex.join(argv), state.box.name)
        )
    config = json_object(data, "config", argv)
    ssh = json_object(config, "ssh", argv)
    user = json_object(config, "user", argv)
    pid = data.get("hostAgentPID")
    forwards = config.get("portForwards", [])
    return LimaInfo(
        host_agent_pid=aws.checked_pid(pid),
        port_forwards=forwards if isinstance(forwards, list) else [],
        mounts=config.get("mounts"),
        forward_agent=ssh.get("forwardAgent") is True,
        status=status,
        over_vsock=ssh.get("overVsock"),
        propagate_proxy_env=config.get("propagateProxyEnv"),
        load_dot_ssh_pub_keys=ssh.get("loadDotSSHPubKeys"),
        user_uid=user.get("uid"),
        ssh_local_port=ssh.get("localPort"),
        vm_type=config.get("vmType"),
    )


def lima_ssh_ports() -> List[int]:
    """The ssh.localPort of every instance limactl lists. A port that is not
    an integer in limactl's own JSON is a listing yo cannot read."""
    argv = limactl("list", "--json")
    completed = proc.checked(proc.run(argv), shlex.join(argv))
    ports: List[int] = []
    for line in completed.stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise YoError(
                "%s printed a line that is not a JSON object: %r"
                % (shlex.join(argv), line[:200])
            )
        config = json_object(data, "config", argv)
        port = json_object(config, "ssh", argv).get("localPort")
        if isinstance(port, bool) or not isinstance(port, int):
            raise YoError(
                "%s printed ssh.localPort %r for %s, not a port number"
                % (shlex.join(argv), port, ui.shown(str(data.get("name"))))
            )
        ports.append(port)
    return ports


def json_object(parent: dict, key: str, argv: Sequence[str]) -> dict:
    """`parent[key]`, which must be a JSON object; a missing key is an empty one.

    A null or a scalar there is a listing yo cannot read, never a box with
    every setting unset: that would report every gap, or none.
    """
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise YoError(
            "%s printed %s as %r, not a JSON object" % (shlex.join(argv), key, value)
        )
    return value


def broken_instance_message(argv: Sequence[str], data: dict) -> str:
    """Why lima reports the instance as Broken, as far as the listing says."""
    errors = data.get("errors")
    said = (
        "; ".join(error for error in errors if isinstance(error, str) and error)
        if isinstance(errors, list)
        else ""
    )
    return (
        "%s reports %s as Broken%s. Lima cannot load the instance, so nothing "
        "starts or stops it until that is mended: `limactl list %s` shows what "
        "lima says, and %s holds its files. Do not delete it to make the message "
        "go away: the disk is in there"
        % (
            shlex.join(argv),
            state.box.name,
            ": " + said if said else " and prints no reason",
            state.box.name,
            state.box.tilde_dir,
        )
    )


def normalize_port_forward(rule: dict) -> dict:
    """A portForwards entry with lima's defaults filled in, as `limactl list --json` shows it.

    Mirrors lima v2.2.1 FillPortForwardDefaults (pkg/limayaml/defaults.go), in
    its order. Only the fields that decide what is mirrored are returned.
    """
    proto = rule.get("proto", "any")
    guest_ip = rule.get("guestIP")
    must_be_zero = rule.get("guestIPMustBeZero", guest_ip == "0.0.0.0")
    if guest_ip is None:
        guest_ip = "0.0.0.0" if must_be_zero else "127.0.0.1"
    guest_port_range = rule.get("guestPortRange")
    if guest_port_range is None:
        guest_port = rule.get("guestPort")
        guest_port_range = (
            [1, MAX_PORT] if guest_port is None else [guest_port, guest_port]
        )
    host_port_range = rule.get("hostPortRange")
    if host_port_range is None:
        host_port = rule.get("hostPort")
        host_port_range = (
            guest_port_range if host_port is None else [host_port, host_port]
        )
    return {
        "proto": proto,
        "guestIP": guest_ip,
        "guestIPMustBeZero": must_be_zero,
        "guestPortRange": guest_port_range,
        "hostIP": rule.get("hostIP", "127.0.0.1"),
        "hostPortRange": host_port_range,
        "ignore": rule.get("ignore", False),
    }


def listener_rules(forwards: Sequence[object], proto: str) -> List[dict]:
    """The ordered, normalized rules that decide which `proto` listeners are mirrored.

    A rule of the other protocol never matches a listener of this one, and
    lima matches a socket rule (guestSocket) against sockets, never against
    listeners. Order counts: lima takes the first matching rule, so a forward
    placed before the catch-all is as much a gap as a missing catch-all.
    """
    rules = [
        normalize_port_forward(forward)
        for forward in forwards
        if isinstance(forward, dict) and "guestSocket" not in forward
    ]
    return [rule for rule in rules if rule["proto"] in (proto, "any")]


def lima_edit_command(expressions: Sequence[str]) -> str:
    """The pasteable chain that applies `expressions` to the stopped instance.

    It starts with `yo down`, not `limactl stop`: lima's stop exits 1 on an
    instance that is already stopped, and the chain must run from either
    state. `--tty=false` declines lima's "start the instance now?" prompt, so
    the start is `yo up`'s, with its unlock.
    """
    return "%s && %s && %s" % (
        state.yo_cmd("down"),
        shlex.join(
            limactl(
                "edit",
                "--tty=false",
                state.box.name,
                "--set",
                " | ".join(expressions),
            )
        ),
        state.yo_cmd("up"),
    )


def refuse_mirrored_ssh_port(port: int) -> None:
    """Refuse an ssh port another box's listener can claim, or that no socket has.

    Lima mirrors every guest TCP port up to MIRRORED_TCP_PORTS[1] onto the
    host's 127.0.0.1, and yo dials the ssh port without checking a host key
    for a root login, the passphrase and the first host-key fetch. The one
    check for the port read from limactl (ssh_port) and the port the listener
    check proves before each of those (verify_vm_listener), so a port past
    the last one is refused here too.
    """
    if MIN_SSH_LOCAL_PORT <= port <= MAX_PORT:
        return
    why = (
        "which another box's listener can claim: lima mirrors every guest TCP "
        "port up to %d onto the host's 127.0.0.1, and yo logs in as root, sends "
        "the disk passphrase and fetches the host key to pin on this port without "
        "checking a host key" % MIRRORED_TCP_PORTS[1]
        if port < MIN_SSH_LOCAL_PORT
        else "which is past the last TCP port, %d" % MAX_PORT
    )
    raise YoError(
        "%s has ssh.localPort %d, %s. Move it to a free port in %d-%d while it is "
        "stopped:\n    %s\nor, because a new box picks its port itself, %s"
        % (
            state.box.name,
            port,
            why,
            MIN_SSH_LOCAL_PORT,
            MAX_PORT,
            lima_edit_command([".ssh.localPort=<PORT>"]),
            state.recreate_chain(),
        )
    )


# What `lsof -F p` prints per process holding a socket: its pid, then a
# line per descriptor of it.
LSOF_PID_RE = re.compile(r"p([0-9]+)")
LSOF_FD_RE = re.compile(r"f[0-9]+")


def tcp_listener_pids(port: int) -> List[int]:
    """The pid of every process listening on TCP `port`, on any address, as
    lsof names them. Only lsof's own lines are an answer: anything else, an
    exit 1 with no output (which is also what lsof says when nothing
    listens) included, is an error in lsof's words."""
    argv = ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fp"]
    listed = proc.checked(proc.run(argv), shlex.join(argv))
    lines = listed.stdout.splitlines()
    pids = [
        int(found.group(1))
        for found in map(LSOF_PID_RE.fullmatch, lines)
        if found is not None
    ]
    if not pids or not all(
        LSOF_PID_RE.fullmatch(line) or LSOF_FD_RE.fullmatch(line) for line in lines
    ):
        raise YoError(
            "`%s` printed no listener's pid but '%s'"
            % (shlex.join(argv), ui.shown(listed.stdout.strip()))
        )
    return pids


def mac_memory_bytes() -> int:
    """The RAM this Mac has, in bytes, as `sysctl -n hw.memsize` says."""
    argv = ["sysctl", "-n", "hw.memsize"]
    said = proc.checked(proc.run(argv), shlex.join(argv)).stdout.strip()
    if not proc.is_ascii_number(said):
        raise YoError("%s printed %r, not a number of bytes" % (shlex.join(argv), said))
    return int(said)


class LimaDriver:
    """macOS: one Lima instance per machine, run by the vz driver."""

    name = "lima"
    instances_dir = ".lima"
    hints = DriverHints(
        keepalive_name="host agent",
        login_fix="lima's key, ~/%s, unchanged since the box was created"
        % LIMA_USER_KEY,
        force_stop_command="limactl stop --force {name}",
        where_to_look="`limactl list` shows its state, lima's hostagent log says "
        "why ({dir}/ha.stderr.log) and the guest's console shows how far it got "
        "({dir}/serialv.log); then run this command again",
    )

    def require_tool(self) -> None:
        proc.require_tool("limactl", "brew install lima")

    def op_sock(self) -> str:
        return os.path.join(
            state.home(),
            "Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock",
        )

    def ssh_facts(self) -> SshFacts:
        return SshFacts(
            self.ssh_port(),
            release.host_user(),
            os.path.join(state.home(), LIMA_USER_KEY),
            "none",
        )

    def forward_port(self, guest_port: int) -> int:
        raise YoError(
            "lima already mirrors the guest's TCP ports %d-%d onto the host's "
            "127.0.0.1; yo port is for Linux hosts" % tuple(MIRRORED_TCP_PORTS),
            code=USAGE_EXIT,
        )

    def ssh_port(self) -> int:
        """Where lima's hostagent listens on the Mac for this instance's sshd.

        The default instance is created from lima/yolobox.yaml, which fixes the port.
        Any other instance has whatever `ssh.localPort` it was created with, and a
        guess would probe a different box: the default one answers on SSH_LOCAL_PORT.
        """
        if state.box.name == DEFAULT_INSTANCE:
            return SSH_LOCAL_PORT
        info = lima_instance_info()
        if info is None:
            raise YoError(
                "instance %s does not exist, so yo cannot learn its ssh port — create "
                "it with: %s" % (state.box.name, state.yo_cmd("up"))
            )
        port = info.ssh_local_port
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 < port <= MAX_PORT
        ):
            raise YoError(
                "%s has ssh.localPort %r, which is not a fixed port. With 0 lima picks a "
                "new port on every start, and yo must know it before the first one — %s"
                % (state.box.name, port, state.recreate_chain())
            )
        refuse_mirrored_ssh_port(port)
        return port

    def keepalive_pid(self) -> Optional[int]:
        info = lima_instance_info()
        return info.host_agent_pid if info else None

    def vm_status(self) -> Optional[VmStatus]:
        try:
            info = lima_instance_info()
        except BrokenInstance:
            return VmStatus(VmState.OTHER, BROKEN)
        if info is None:
            return None
        if not isinstance(info.status, str) or not info.status:
            # The status is the word a VmStatus shows; this is no answer to show.
            raise YoError(
                "limactl list %s --json printed the status %r, not a word"
                % (state.box.name, info.status)
            )
        states = {"Running": VmState.RUNNING, "Stopped": VmState.OFF}
        return VmStatus(states.get(info.status, VmState.OTHER), info.status)

    def creation_refusal(self, size: Size) -> Optional[str]:
        # create() gives a named instance its own port, so any size is created.
        return None

    def start_refusal(self) -> Optional[str]:
        # start() judges the instance's own settings before it changes anything.
        return None

    def resize_refusal(self, size: Size) -> Optional[str]:
        if size.memory_gib is None:
            return None
        ram = mac_memory_bytes()
        if size.memory_gib * GIB <= ram:
            return None
        return (
            "--memory %d is more than the %d GiB of memory this Mac has (sysctl "
            "hw.memsize): ask for at most --memory %d"
            % (size.memory_gib, ram // GIB, ram // GIB)
        )

    def resume_hint(self, word: str) -> Optional[str]:
        return None

    def disk_gib(self) -> int:
        # The Disk field of limactl's own instance record, in bytes.
        argv = limactl("list", state.box.name, "--format", "{{.Disk}}")
        probe = proc.checked(proc.run(argv), shlex.join(argv))
        disk = probe.stdout.strip()
        if not proc.is_ascii_number(disk):
            raise YoError(
                "limactl reports no disk size for %s — does the instance exist? (%s)"
                % (state.box.name, state.yo_cmd("status"))
            )
        return driver.exact_gib(int(disk), "disk")

    def config_gaps(self) -> List[str]:
        info = lima_instance_info()
        if info is None:
            return []
        gaps: List[str] = []
        if any(
            isinstance(forward, dict) and "guestSocket" in forward
            for forward in info.port_forwards
        ):
            gaps.append(GAP_REVERSE_SOCKETS)
        for proto, gap in (("udp", GAP_UDP_FORWARDS), ("tcp", GAP_TCP_FORWARDS)):
            if listener_rules(info.port_forwards, proto) != listener_rules(
                LIMA_PORT_FORWARDS, proto
            ):
                gaps.append(gap)
        # Lima omits an empty mounts list; anything but a list is not "none".
        if info.mounts not in (None, []):
            gaps.append(GAP_MOUNTS)
        if info.forward_agent:
            gaps.append(GAP_FORWARD_AGENT)
        if info.over_vsock is not False:
            gaps.append(GAP_SSH_OVER_VSOCK)
        if info.propagate_proxy_env is not False:
            gaps.append(GAP_PROPAGATE_PROXY_ENV)
        if info.load_dot_ssh_pub_keys is not False:
            gaps.append(GAP_LOAD_DOT_SSH_PUB_KEYS)
        # Both read only at creation: the uid decides whose files the operator's
        # are, and only vz has the rosetta share.
        if info.user_uid != OPERATOR_GUEST_UID:
            gaps.append(GAP_OPERATOR_UID)
        if info.vm_type != LIMA_VM_TYPE:
            gaps.append(GAP_VM_TYPE)
        return gaps

    def refuse_fatal_gaps(self) -> None:
        """Refuse an instance whose settings break the box's promises; warn
        about the rest, which leak but lock nobody out."""
        gaps = self.config_gaps()
        if any(gap in LIMA_FATAL_GAPS for gap in gaps):
            raise YoError(self.config_gap_message(gaps))
        if gaps:
            ui.err("yo: %s" % self.config_gap_message(gaps))

    def config_gap_message(self, gaps: Sequence[str]) -> str:
        """Each gap with what it does, then the one remedy for them all: a
        `limactl edit` of the stopped instance, or, when a gap is a setting
        lima reads only at creation, the instance made anew."""
        known = {gap: LIMA_GAPS[gap] for gap in gaps}
        lines = ["instance %s departs from lima/yolobox.yaml:" % state.box.name]
        lines += ["    %s: %s" % (gap, problem) for gap, (problem, _) in known.items()]
        edits = list(dict.fromkeys(edit for _, edit in known.values()))
        if None in edits:
            lines.append(
                "lima reads %s only when it creates an instance, so copy out what "
                "you need first, then %s"
                % (
                    " and ".join(
                        gap for gap, (_, edit) in known.items() if edit is None
                    ),
                    state.recreate_chain(),
                )
            )
        else:
            lines.append(
                "fix it while it is stopped: %s"
                % lima_edit_command([edit for edit in edits if edit is not None])
            )
        return "\n".join(lines)

    def verify_vm_listener(self, port: int) -> None:
        """Prove that this instance's hostagent, and nothing else, listens on
        the box's ssh port: below the mirrored range's end another box's
        listener could claim it, and any process of the host user's could
        bind it while nothing held it."""
        refuse_mirrored_ssh_port(port)
        hostagent = self.keepalive_pid()
        try:
            holders = tcp_listener_pids(port)
        except YoError as unproved:
            raise YoError(
                "cannot prove that %s's hostagent holds %s:%d, so yo does not "
                "connect: %s" % (state.box.name, LOOPBACK, port, unproved.message)
            )
        strangers = [pid for pid in holders if pid != hostagent]
        if strangers:
            raise YoError(
                "%s:%d is held by pid %s, not by %s's hostagent (%s), so yo does not "
                "connect. Find the holder with `lsof -nP -iTCP:%d -sTCP:LISTEN`, "
                "stop it, and run the command again"
                % (
                    LOOPBACK,
                    port,
                    ", ".join(map(str, strangers)),
                    state.box.name,
                    "pid %d" % hostagent if hostagent is not None else "none runs",
                    port,
                )
            )

    def create(self, held: state.HeldBox, new_box: NewBox) -> None:
        # The lock spans the pick and `limactl create`, which writes the port
        # into the instance's record, where the next yo's pick sees it.
        with driver.port_lock(os.path.join(state.home(), self.instances_dir)):
            argv = limactl(
                "create",
                "--tty=false",
                "--name",
                state.box.name,
                *self.ssh_port_flags(),
                *new_box.flags(),
                str(SCRIPT_DIR / "lima" / "yolobox.yaml"),
            )
            proc.checked(proc.run(argv, capture=False), shlex.join(argv))

    def ssh_port_flags(self) -> List[str]:
        """What gives a new instance its ssh.localPort: lima/yolobox.yaml fixes
        the default instance's; any other gets the lowest port above the
        mirrored range that no instance claims, the default one's included,
        and nothing on the host holds. An instance whose port is 0 claims none
        until it starts, and yo refuses to drive it."""
        if state.box.name == DEFAULT_INSTANCE:
            return []
        port = driver.lowest_free_port(
            range(MIN_SSH_LOCAL_PORT, MAX_PORT + 1),
            [SSH_LOCAL_PORT, *lima_ssh_ports()],
            driver.loopback_port_bound,
        )
        return ["--set", ".ssh.localPort=%d" % port]

    def start(self, held: state.HeldBox, passphrase: Callable[[], bytes]) -> None:
        self.refuse_fatal_gaps()
        # limactl start blocks until lima's own ssh works, which needs the disk
        # unlocked, so it runs beside the unlock instead of before it, and its
        # own deadline must outlast the seed's in-place encryption.
        starter = proc.spawn(
            limactl(
                "start", "--tty=false", "--timeout", LIMA_START_TIMEOUT, state.box.name
            )
        )
        try:
            self.wait_until_running(starter)
            unlock.unlock_disk(held, passphrase)
            rc = starter.wait()
            if rc != 0:
                raise YoError(
                    "limactl start exited %d — the VM may still be running; check: limactl list"
                    % rc
                )
            unlock.wait_for_operator()
        except BaseException:
            # The hostagent is a separate process and survives this.
            starter.terminate()
            starter.wait()
            raise

    def wait_until_running(self, starter: subprocess.Popen) -> None:
        """Poll limactl until it reports the instance Running; `limactl start`
        exiting first, or any status but a pre-boot one, ends the wait."""
        while True:
            status = self.vm_status()
            if status is not None and status.state is VmState.RUNNING:
                return
            if starter.poll() is not None:
                raise YoError(
                    "limactl start exited %d before %s was Running"
                    % (starter.returncode, state.box.name)
                )
            if status is not None and status.word not in PRE_BOOT_STATUSES:
                raise unlock.vm_lost_error(status, "lima to report it Running")
            time.sleep(unlock.INITRD_POLL_SECONDS)

    def stop(self, held: state.HeldBox) -> None:
        # An instance that is not there is left to limactl to refuse.
        status = self.vm_status()
        if status is not None and status.state is VmState.OFF:
            return
        # `yo down` reaches this without proof the box is past its initrd, so
        # the hostagent's kill, which ends the VM, is offered only once `yo ssh
        # true` succeeds: the initrd refuses that login (with a host-key or a
        # permission error), and only stage 2 runs `true` and exits 0.
        self.limactl_stop(
            [state.box.name],
            "limactl stop hangs on the vz driver sometimes. If %s "
            "succeeds (exits 0 and prints nothing), the box is past its initrd: "
            "recover with %s, then kill -9 any leftover 'limactl hostagent' for %s, "
            "then %s. If it fails in any way, the box may be in its initrd, "
            "encrypting its disk: run %s, and do not kill the hostagent"
            % (
                state.yo_cmd("ssh", "true"),
                state.yo_cmd("ssh", "sudo", "systemctl", "reboot"),
                state.box.name,
                state.yo_cmd("up"),
                state.yo_cmd("status"),
            ),
        )

    def force_stop(self, held: state.HeldBox) -> None:
        # The box is in its initrd, where `yo ssh` cannot reach it, so the
        # recovery is the hostagent's kill alone (under vz that ends the VM).
        self.limactl_stop(
            ["--force", state.box.name],
            "limactl stop hangs on the vz driver sometimes; recover with: "
            "kill -9 any 'limactl hostagent' for %s (that also ends the VM) and any "
            "child it left spinning, then %s" % (state.box.name, state.yo_cmd("up")),
        )

    def limactl_stop(self, args: List[str], hang_recovery: str) -> None:
        argv = limactl("stop", *args)
        try:
            completed = proc.run(argv, capture=False, timeout=LIMA_STOP_TIMEOUT)
        except RunTimeout as failure:
            raise StopRefused(failure.message, hang_recovery)
        if completed.returncode == 0:
            return
        # Its output went to the terminal, so the message carries the exit only.
        failed = proc.failed_message(shlex.join(argv), completed.returncode, None)
        # A stop that gave up on a box still running needs the hang's recovery;
        # one that failed on a box lima reports as not running is only a failure.
        status = self.vm_status()
        if status is not None and status.state is VmState.RUNNING:
            raise StopRefused(failed, hang_recovery)
        raise YoError(failed)

    def removals(self) -> List[Removal]:
        if not lima_instance_exists():
            return []
        return [
            Removal(
                "delete the Lima instance %s and %s, powering it off first if it runs"
                % (state.box.name, state.box.tilde_dir),
                self.delete_instance,
            )
        ]

    def delete_instance(self, held: state.HeldBox) -> None:
        proc.run(
            limactl("delete", "--force", state.box.name), capture=False, check=True
        )

    def show_status(self) -> Optional[str]:
        argv = limactl("list", state.box.name)
        listed = proc.run(argv, capture=False)
        if listed.returncode == 0:
            return None
        return "lima: %s, its own message is above" % proc.failed_message(
            shlex.join(argv), listed.returncode, None
        )

    def disk_image(self) -> Optional[Tuple[str, int]]:
        kib = du_kib(os.path.join(state.instance_dir(), "disk"))
        if kib is None:
            return None
        return "%s/disk" % state.box.tilde_dir, kib

    def edit_instance(self, flags: Sequence[str]) -> int:
        """One `limactl edit` of the stopped instance, which lima validates and
        then writes into lima.yaml in place (a crash inside that write may leave
        a file lima reads as Broken). It runs in the instance's directory, where
        lima leaves an edit it rejected; the exit code is returned."""
        argv = limactl("edit", "--tty=false", state.box.name, *flags)
        return proc.run(argv, capture=False, cwd=state.instance_dir()).returncode

    def grow_disk(self, held: state.HeldBox, wanted_gib: int) -> None:
        ui.out("lima: raising %s's disk to %dGiB" % (state.box.name, wanted_gib))
        code = self.edit_instance(["--disk", str(wanted_gib)])
        if code != 0:
            raise YoError("limactl edit refused the disk resize (exit %d)" % code)

    def resize(self, held: state.HeldBox, size: Size) -> None:
        flags = Size(size.memory_gib, None, size.cpus).flags()
        code = self.edit_instance(flags)
        if code != 0:
            raise YoError(
                "limactl edit refused the resize (exit %d), so %s keeps its old size"
                % (code, state.box.name)
            )
        ui.out("lima: resized %s to %s" % (state.box.name, " ".join(flags)))


LIMA = LimaDriver()


def du_kib(path: str) -> Optional[int]:
    """Host KiB the file allocates; None when there is no such file."""
    try:
        return os.stat(path).st_blocks * STAT_BLOCK_BYTES // KIB
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise YoError("cannot stat %s: %s" % (path, ui.os_error_reason(failure)))
