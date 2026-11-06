"""The Lima driver: the instance, its config gaps and its lifecycle."""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import aws, driver, proc, state, ui, unlock
from .config import (
    AWS_BROKER_GUEST_SOCK,
    DEFAULT_INSTANCE,
    KIB,
    MAX_PORT,
    MIB,
    OP_GUEST_SOCK,
    OPERATOR_GUEST_UID,
    SCRIPT_DIR,
    USAGE_EXIT,
)
from .driver import NO_SIZE, BoxSize, Removal, Size, StopRefused
from .ui import RunTimeout, YoError


# LimaDriver.config_gaps() entries that are not a missing reverse forward's guest
# socket: a setting on the instance that breaks the no-host-mounts promise.
GAP_MOUNTS = "mounts"
GAP_FORWARD_AGENT = "ssh.forwardAgent"
GAP_SSH_OVER_VSOCK = "ssh.overVsock"
GAP_PROPAGATE_PROXY_ENV = "propagateProxyEnv"
GAP_LOAD_DOT_SSH_PUB_KEYS = "ssh.loadDotSSHPubKeys"
GAP_UDP_FORWARDS = "portForwards.udp"
GAP_TCP_FORWARDS = "portForwards.tcp"
GAP_OPERATOR_UID = "user.uid"

# The guest TCP ports lima mirrors onto the host's 127.0.0.1: unprivileged and
# below the macOS ephemeral range (49152). A Linux host mirrors nothing: it has no
# fixed range to give away, so `yo port` hands out host ports from
# host_port_window(), the ports above the kernel's ephemeral range.
MIRRORED_TCP_PORTS = [1024, 49151]

# The lowest port a non-default instance may use for ssh.localPort: above every
# mirrored guest port. The initrd unlock sends the passphrase to that port
# without checking the host key, so a port inside the mirror could be answered
# by a listener of another running box.
MIN_SSH_LOCAL_PORT = MIRRORED_TCP_PORTS[1] + 1

# Fixed OAuth callback ports inside MIRRORED_TCP_PORTS that are never mirrored:
# Codex CLI 1455-1457, Databricks 8020, gcloud 8085, Firebase 9005, kubelogin 18000.
# (kubelogin's 8000 stays mirrored on purpose: it is also the Django and
# http.server default.)
OAUTH_CALLBACK_PORT_RANGES = [
    [1455, 1457],
    [8020, 8020],
    [8085, 8085],
    [9005, 9005],
    [18000, 18000],
]

# The order matters: lima takes the first matching rule, so the deny-list comes
# before the range and the catch-alls come last. See lima/yolobox.md.
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
    {
        "guestSocket": OP_GUEST_SOCK,
        "hostSocket": "{{.Home}}/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock",
        "reverse": True,
    },
    {
        "guestSocket": AWS_BROKER_GUEST_SOCK,
        "hostSocket": "{{.Home}}/.local/state/yolobox/aws-broker.sock",
        "reverse": True,
    },
    {"proto": "udp", "guestIP": "0.0.0.0", "guestIPMustBeZero": False, "ignore": True},
    {"proto": "tcp", "guestIP": "0.0.0.0", "guestIPMustBeZero": False, "ignore": True},
]

RESTATE_PORT_FORWARDS = ".portForwards = %s" % json.dumps(LIMA_PORT_FORWARDS)

LIMA_SETTING_GAPS = {
    GAP_MOUNTS: ("mounts host directories into the VM", ".mounts = []"),
    GAP_FORWARD_AGENT: (
        "forwards the host's ssh agent to every lima ssh session",
        ".ssh.forwardAgent = false",
    ),
    GAP_SSH_OVER_VSOCK: (
        "lets lima proxy ssh over vsock, which the initrd that unlocks the disk cannot answer, "
        "and which locks the operator out of the booted box: its sshd admits the operator only "
        "from 192.168.5.2, and a vsock peer has no IP address",
        ".ssh.overVsock = false",
    ),
    GAP_PROPAGATE_PROXY_ENV: (
        "copies the host's HTTP(S)_PROXY into the VM, which can point the guest at a proxy on the host's loopback: an entrance to the host nobody controls",
        ".propagateProxyEnv = false",
    ),
    GAP_LOAD_DOT_SSH_PUB_KEYS: (
        "adds the host's ~/.ssh public keys to the VM's authorized_keys",
        ".ssh.loadDotSSHPubKeys = false",
    ),
    GAP_UDP_FORWARDS: (
        "mirrors guest UDP listeners onto the host's localhost (including DHCP's 68 and tailscaled's 41641, which collides with the host's own Tailscale)",
        RESTATE_PORT_FORWARDS,
    ),
    GAP_TCP_FORWARDS: (
        "mirrors guest TCP listeners onto the host's localhost beyond the dev ports 1024–49151 (or publishes one beyond loopback)",
        RESTATE_PORT_FORWARDS,
    ),
}

# Gaps that no `limactl edit` mends: lima reads the setting once, when it creates
# the instance, so only a new instance has it right. A note and not a fatal gap:
# the operator must still be able to start the box to copy their work out.
LIMA_RECREATE_GAPS = {
    GAP_OPERATOR_UID: (
        "has an operator whose uid is not %d (lima took the Mac account's uid), so "
        "its files do not match the account nix/base.nix declares. Lima reads "
        "user.uid only when it creates an instance, and `limactl edit` cannot "
        "change file ownership. Push out what you need, then recreate the "
        "instance from lima/yolobox.yaml, which pins the uid" % OPERATOR_GUEST_UID
    ),
}

# The gaps vm_up refuses to start with. Every other gap is a note: the healing
# step adds a missing forward live, and the rest leak but lock nobody out. Over
# vsock, the initrd that unlocks the disk cannot answer, and the guest's sshd
# admits the operator only from 192.168.5.2, which a vsock peer, having no IP
# address, never is (nix/base.md).
LIMA_FATAL_GAPS = frozenset([GAP_SSH_OVER_VSOCK])

# ssh.localPort in lima/yolobox.yaml: where lima's hostagent listens on the Mac
# for the default instance. state.driver().ssh_port() answers for any instance.
SSH_LOCAL_PORT = 60022

LIMA_STOP_TIMEOUT = 300

# st_blocks counts blocks of this size whatever the filesystem's own is (POSIX).
STAT_BLOCK_BYTES = 512


# The host drivers. Everything from here to the libvirt block is the macOS side:
# what limactl reports and does, behind LimaDriver. Callers outside this block
# reach it only through state.driver().
@dataclass(frozen=True)
class LimaInfo:
    host_agent_pid: Optional[int]
    port_forwards: List[dict]
    mounts: list
    forward_agent: bool
    status: str
    over_vsock: Optional[bool]
    propagate_proxy_env: Optional[bool]
    load_dot_ssh_pub_keys: Optional[bool]
    user_uid: object  # the operator's uid as lima created it
    ssh_local_port: object  # as limactl reports it: 0 means lima picks per start


def lima_instance_exists() -> bool:
    """Whether limactl knows this machine's name: the one place that decides it.

    A failing `limactl list -q` is an error, never "no instance": a box whose
    limactl is broken would otherwise look like a new one to create.
    """
    argv = ["limactl", "list", "-q"]
    completed = proc.run(argv, check=False)
    if completed.returncode != 0:
        raise YoError(
            "%s failed (exit %d): %s"
            % (shlex.join(argv), completed.returncode, proc.failure_detail(completed))
        )
    return state.box.name in completed.stdout.splitlines()


def lima_instance_info() -> Optional[LimaInfo]:
    """What limactl reports of this machine; None when there is no such instance.

    Any failure to read an instance that exists is an error, never a missing
    instance: a box whose limactl is broken would otherwise look like one with
    no gaps.
    """
    if not lima_instance_exists():
        return None
    argv = ["limactl", "list", state.box.name, "--json"]
    completed = proc.run(argv, check=False)
    if completed.returncode != 0:
        raise YoError(
            "%s failed (exit %d): %s"
            % (shlex.join(argv), completed.returncode, proc.failure_detail(completed))
        )
    try:
        data = json.loads(completed.stdout.strip())
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise YoError(
            "%s did not print one JSON object: %r"
            % (shlex.join(argv), completed.stdout.strip()[:200])
        )
    pid = data.get("hostAgentPID")
    config = data.get("config", {})
    ssh = config.get("ssh", {})
    user = config.get("user", {})
    forwards = config.get("portForwards", [])
    mounts = config.get("mounts", [])
    return LimaInfo(
        host_agent_pid=aws.checked_pid(pid),
        port_forwards=forwards if isinstance(forwards, list) else [],
        mounts=mounts if isinstance(mounts, list) else [],
        forward_agent=ssh.get("forwardAgent") is True,
        status=data.get("status", ""),
        over_vsock=ssh.get("overVsock"),
        propagate_proxy_env=config.get("propagateProxyEnv"),
        load_dot_ssh_pub_keys=ssh.get("loadDotSSHPubKeys"),
        user_uid=user.get("uid"),
        ssh_local_port=ssh.get("localPort"),
    )


def ignores_all_guest_udp(forward: object) -> bool:
    """Whether a normalized portForwards entry ignores UDP listeners on every address.

    Lima's matcher skips an ignore rule for a listener bound to 0.0.0.0 or ::
    unless the rule's own guestIP is unspecified too, and it matches a listener
    on a specific address only when the rule's guestIPMustBeZero is false
    (see lima/yolobox.md, "Guest UDP is never mirrored").
    """
    if not isinstance(forward, dict):
        return False
    guest_ip = forward.get("guestIP")
    try:
        unspecified = (
            isinstance(guest_ip, str) and ipaddress.ip_address(guest_ip).is_unspecified
        )
    except ValueError:
        unspecified = False
    return (
        forward.get("ignore") is True
        and forward.get("proto") == "udp"
        and forward.get("guestPortRange") == [1, MAX_PORT]
        and unspecified
        and forward.get("guestIPMustBeZero") is False
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


def tcp_forward_rules(forwards: Sequence[object]) -> List[dict]:
    """The ordered, normalized rules that decide which TCP listeners are mirrored.

    The reverse socket rules have their own checks and every UDP rule has its
    own gap. Order counts: lima takes the first matching rule.
    """
    rules = [
        normalize_port_forward(forward)
        for forward in forwards
        if isinstance(forward, dict) and "guestSocket" not in forward
    ]
    return [rule for rule in rules if rule["proto"] != "udp"]


def lima_edit_command(expression: str) -> str:
    return "limactl stop %s && limactl edit %s --set '%s' && %s" % (
        state.box.name,
        state.box.name,
        expression,
        state.yo_cmd("up"),
    )


class LimaDriver:
    """macOS: one Lima instance per machine, run by the vz driver."""

    name = "lima"
    instances_dir = ".lima"
    initrd_identity = ".lima/_config/user"
    start_command = "limactl start"
    off_status = "Stopped"

    def require_tool(self) -> None:
        proc.require_tool("limactl", "brew install lima")

    def op_sock(self) -> str:
        return os.path.join(
            state.home(),
            "Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock",
        )

    def initrd_identity_agent(self) -> str:
        return "none"

    def require_login_key(self) -> None:
        pass  # lima's own key file under ~/.lima logs in; no agent is involved

    def unknown_next_step(self) -> str:
        # serialv.log keeps the whole boot, unlike a live console.
        return (
            "If the login is right already, its boot log tells which: %s. While "
            "'%s' has started and not finished there, the initrd is encrypting the "
            "disk; leave it running. A login prompt there means a stage 2 built "
            "before the banner mark: rebuild it with %s once %s succeeds"
            % (
                self.boot_log(),
                unlock.MIGRATE_UNIT_DESCRIPTION,
                state.yo_cmd("bootstrap"),
                state.yo_cmd("ssh", "true"),
            )
        )

    def login_fix(self) -> str:
        return "lima's key, ~/%s, unchanged since the box was created" % (
            self.initrd_identity
        )

    def forward_port(self, guest_port: int) -> int:
        raise YoError(
            "lima already mirrors the guest's TCP ports %d-%d onto the host's "
            "127.0.0.1; yo port is for Linux hosts" % tuple(MIRRORED_TCP_PORTS),
            code=USAGE_EXIT,
        )

    def create_command(self, size: Size) -> str:
        # lima/yolobox.yaml fixes the default instance's port; any other needs its own.
        port = (
            []
            if state.box.name == DEFAULT_INSTANCE
            else [
                "--set",
                ".ssh.localPort=<a free port in %d-%d>"
                % (MIN_SSH_LOCAL_PORT, MAX_PORT),
            ]
        )
        return shlex.join(
            [
                "limactl",
                "create",
                "--name",
                state.box.name,
                *port,
                *size.flags(),
                str(SCRIPT_DIR / "lima" / "yolobox.yaml"),
            ]
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
                "it with:\n    %s" % (state.box.name, self.create_command(NO_SIZE))
            )
        port = info.ssh_local_port
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 < port <= MAX_PORT
        ):
            raise YoError(
                "%s has ssh.localPort %r, which is not a fixed port. With 0 lima picks a "
                "new port on every start, and yo must know it before the first one. "
                "Create the instance with:\n    %s"
                % (state.box.name, port, self.create_command(NO_SIZE))
            )
        if port < MIN_SSH_LOCAL_PORT:
            raise YoError(
                "%s has ssh.localPort %d, which another box's listener can claim: lima "
                "mirrors every guest TCP port up to %d onto the host's 127.0.0.1, and "
                "the disk unlock sends the passphrase to this port without checking "
                "the host key. Stop the instance and move it to a port in %d-%d:\n    %s"
                % (
                    state.box.name,
                    port,
                    MIRRORED_TCP_PORTS[1],
                    MIN_SSH_LOCAL_PORT,
                    MAX_PORT,
                    lima_edit_command(
                        ".ssh.localPort=<a free port in %d-%d>"
                        % (MIN_SSH_LOCAL_PORT, MAX_PORT)
                    ),
                )
            )
        return port

    def keepalive_pid(self) -> Optional[int]:
        info = lima_instance_info()
        return info.host_agent_pid if info else None

    def keepalive_pid_error(self) -> str:
        return state.not_up_hint(
            "cannot read %s's host agent pid (%s %s --json)"
            % (state.box.name, self.list_command(), state.box.name)
        )

    def vm_status(self) -> Optional[str]:
        info = lima_instance_info()
        return info.status if info is not None else None

    def creation_refusal(self, size: Size) -> Optional[str]:
        # Only the default instance is created here, from lima/yolobox.yaml, which
        # fixes its port. Any other needs a port yo can know, which only its
        # creator can choose.
        if state.box.name == DEFAULT_INSTANCE:
            return None
        rerun = (
            "\nthen run this command again without %s" % " ".join(size.flags())
            if size.asked
            else ""
        )
        # The command carries the CPU count the box would get, since lima's own
        # default is not yo's.
        return "instance %s does not exist — create it with:\n    %s%s" % (
            state.box.name,
            self.create_command(size.for_creation()),
            rerun,
        )

    def current_size(self) -> BoxSize:
        # Go template fields of limactl's own instance record (limactl list
        # --list-fields): Memory and Disk in bytes, CPUs a count.
        argv = [
            "limactl",
            "list",
            state.box.name,
            "--format",
            "{{.Memory}} {{.Disk}} {{.CPUs}}",
        ]
        probe = proc.run(argv)
        if probe.returncode != 0:
            raise YoError(
                "%s failed (exit %d): %s"
                % (shlex.join(argv), probe.returncode, proc.failure_detail(probe))
            )
        fields = probe.stdout.split()
        if len(fields) != 3 or not all(proc.is_ascii_number(f) for f in fields):
            raise YoError(
                "limactl reports no memory, disk and CPUs for %s — does the instance "
                "exist? (%s)" % (state.box.name, state.yo_cmd("status"))
            )
        memory, disk, cpus = (int(field) for field in fields)
        return BoxSize(
            driver.exact_gib(memory, "memory"), driver.exact_gib(disk, "disk"), cpus
        )

    def resize_refusal(self, current: BoxSize, wanted: BoxSize) -> Optional[str]:
        # lima does not look at the host's memory when it creates one either
        return None

    def resize(self, memory_gib: int, cpus: int) -> None:
        edit = proc.run(
            [
                "limactl",
                "edit",
                "--tty=false",
                state.box.name,
                "--memory",
                str(memory_gib),
                "--cpus",
                str(cpus),
            ],
            capture=False,
        )
        if edit.returncode != 0:
            raise YoError(
                "limactl edit refused the resize (exit %d), so %s is left stopped "
                "at its old size" % (edit.returncode, state.box.name)
            )

    def config_gaps(self) -> List[str]:
        info = lima_instance_info()
        if info is None:
            return []
        expected = {
            OP_GUEST_SOCK: self.op_sock(),
            AWS_BROKER_GUEST_SOCK: aws.aws_broker_sock(),
        }
        gaps = [
            guest_sock
            for guest_sock, host_sock in expected.items()
            if not any(
                isinstance(forward, dict)
                and forward.get("reverse") is True
                and forward.get("guestSocket") == guest_sock
                and forward.get("hostSocket") == host_sock
                for forward in info.port_forwards
            )
        ]
        if not any(ignores_all_guest_udp(forward) for forward in info.port_forwards):
            gaps.append(GAP_UDP_FORWARDS)
        if tcp_forward_rules(info.port_forwards) != tcp_forward_rules(
            LIMA_PORT_FORWARDS
        ):
            gaps.append(GAP_TCP_FORWARDS)
        if info.mounts:
            gaps.append(GAP_MOUNTS)
        if info.forward_agent:
            gaps.append(GAP_FORWARD_AGENT)
        if info.over_vsock is not False:
            gaps.append(GAP_SSH_OVER_VSOCK)
        if info.propagate_proxy_env is not False:
            gaps.append(GAP_PROPAGATE_PROXY_ENV)
        if info.load_dot_ssh_pub_keys is not False:
            gaps.append(GAP_LOAD_DOT_SSH_PUB_KEYS)
        if info.user_uid != OPERATOR_GUEST_UID:
            gaps.append(GAP_OPERATOR_UID)
        return gaps

    def fatal_gaps(self, gaps: Sequence[str]) -> List[str]:
        return [gap for gap in gaps if gap in LIMA_FATAL_GAPS]

    def host_gaps(self) -> List[str]:
        return []

    def config_gap_message(self, gaps: Sequence[str]) -> str:
        missing = [
            gap
            for gap in gaps
            if gap not in LIMA_SETTING_GAPS and gap not in LIMA_RECREATE_GAPS
        ]
        lines = []
        restated = bool(missing)
        if missing:
            lines.append(
                "%s has no reverse forward for %s — restate the full array:\n    %s"
                % (
                    state.box.name,
                    " and ".join(missing),
                    lima_edit_command(RESTATE_PORT_FORWARDS),
                )
            )
        for gap in gaps:
            if gap in LIMA_RECREATE_GAPS:
                lines.append(
                    "%s %s:\n    %s"
                    % (
                        state.box.name,
                        LIMA_RECREATE_GAPS[gap],
                        state.destroy_then_bootstrap_command(),
                    )
                )
                continue
            if gap not in LIMA_SETTING_GAPS:
                continue
            problem, expression = LIMA_SETTING_GAPS[gap]
            if expression == RESTATE_PORT_FORWARDS and restated:
                lines.append(
                    "%s %s (the restate above fixes it too)" % (state.box.name, problem)
                )
                continue
            restated = restated or expression == RESTATE_PORT_FORWARDS
            lines.append(
                "%s %s:\n    %s"
                % (state.box.name, problem, lima_edit_command(expression))
            )
        return "\n".join(lines)

    def verify_initrd_listener(self, port: int) -> None:
        pass  # lima's hostagent, which owns the port, runs as the host user

    def start(self, passphrase: bytes, size: Size) -> None:
        if lima_instance_exists():
            start_argv = ["limactl", "start", "--tty=false", state.box.name]
        else:
            start_argv = [
                "limactl",
                "start",
                "--name",
                state.box.name,
                "--tty=false",
                *size.flags(),
                str(SCRIPT_DIR / "lima" / "yolobox.yaml"),
            ]
        # limactl start blocks until lima's own ssh works, which needs the disk
        # unlocked, so it runs beside the unlock instead of before it.
        starter = proc.spawn(start_argv)
        try:
            migrated = unlock.unlock_disk(passphrase, starter)
            rc = starter.wait()
            if rc != 0 and migrated:
                ui.err(
                    "yo: lima gave up waiting while the disk was being encrypted; "
                    "restarting the instance cleanly"
                )
                self.stop()
                starter = proc.spawn(start_argv)
                unlock.unlock_disk(passphrase, starter)
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

    def stop(self) -> None:
        # An instance that is not there is left to limactl to refuse.
        if self.vm_status() == self.off_status:
            return
        # `yo down` reaches this without proof the box is past its initrd, so
        # the hostagent's kill, which ends the VM, is offered only once `yo ssh
        # true` succeeds: the initrd refuses that login (with a host-key or a
        # permission error), and only stage 2 runs `true` and exits 0.
        self.limactl_stop(
            [state.box.name],
            "limactl stop hangs on the vz driver sometimes (TODO.md). If %s "
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

    def force_stop(self) -> None:
        # The box is in its initrd, where `yo ssh` cannot reach it, so the
        # recovery is the hostagent's kill alone (under vz that ends the VM).
        self.limactl_stop(
            ["--force", state.box.name],
            "limactl stop hangs on the vz driver sometimes (TODO.md); recover with: "
            "kill -9 any 'limactl hostagent' for %s (that also ends the VM) and any "
            "child it left spinning, then %s" % (state.box.name, state.yo_cmd("up")),
        )

    def force_stop_command(self) -> str:
        return "limactl stop --force %s" % state.box.name

    def limactl_stop(self, args: List[str], hang_recovery: str) -> None:
        try:
            proc.run(
                ["limactl", "stop", *args],
                capture=False,
                check=True,
                timeout=LIMA_STOP_TIMEOUT,
            )
        except RunTimeout as failure:
            raise StopRefused(failure.message, hang_recovery)

    def stop_command(self) -> str:
        return "limactl stop %s" % state.box.name

    def stop_failed(self, detail: str) -> str:
        return "limactl stop failed: %s" % detail

    def stop_recovery(self) -> List[str]:
        return [
            "Recover with: %s" % self.stop_command(),
            "If the stop hangs too: %s, and kill -9 any leftover"
            % state.yo_cmd("ssh", "sudo", "systemctl", "reboot"),
            "`limactl hostagent` process for %s." % state.box.name,
        ]

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

    def delete_instance(self) -> None:
        proc.run(
            ["limactl", "delete", "--force", state.box.name], capture=False, check=True
        )

    def list_command(self) -> str:
        return "limactl list"

    def shown_instance_file(self, name: str) -> str:
        """A file of the instance directory, spelled from ~ for a printed hint."""
        return "~/%s/%s/%s" % (self.instances_dir, state.box.name, name)

    def boot_log(self) -> str:
        # Lima's own: serialv.log keeps the whole boot, unlike a live console.
        return self.shown_instance_file("serialv.log")

    def initrd_unseen_hint(self) -> str:
        return "its boot log shows how far it got: %s" % self.boot_log()

    def vm_lost_hint(self, status: Optional[str]) -> str:
        if status is None:
            return "instance %s no longer exists — %s" % (
                state.box.name,
                "%s creates it again" % state.yo_cmd("up")
                if state.box.name == DEFAULT_INSTANCE
                else "create it with: %s" % self.create_command(NO_SIZE),
            )
        return (
            "%s shows its state, lima's hostagent log says why: %s, and the guest's "
            "console shows how far it got: %s; then run this command again"
            % (
                self.list_command(),
                self.shown_instance_file("ha.stderr.log"),
                self.boot_log(),
            )
        )

    def show_status(self) -> Optional[str]:
        argv = ["limactl", "list", state.box.name]
        listed = proc.run(argv, capture=False)
        if listed.returncode == 0:
            return None
        return "lima: %s failed (exit %d), its own message is above" % (
            shlex.join(argv),
            listed.returncode,
        )

    def disk_image(self) -> Optional[Tuple[str, int]]:
        kib = du_kib(os.path.join(state.instance_dir(), "disk"))
        if kib is None:
            return None
        return self.shown_instance_file("disk"), kib

    def grow_disk(self, current_gib: int, wanted_gib: int) -> None:
        free_kib = (
            shutil.disk_usage(os.path.join(state.home(), self.instances_dir)).free
            // KIB
        )
        image_kib = du_kib(os.path.join(state.instance_dir(), "disk")) or 0
        ui.out(
            "host: ~/%s has %dGiB free; the disk image allocates %dGiB today"
            % (self.instances_dir, free_kib // MIB, image_kib // MIB)
        )
        ui.out(
            "lima: raising %s from %dGiB to %dGiB"
            % (state.box.name, current_gib, wanted_gib)
        )

        resize = proc.run(
            [
                "limactl",
                "edit",
                "--tty=false",
                state.box.name,
                "--disk",
                str(wanted_gib),
            ],
            capture=False,
        )
        if resize.returncode != 0:
            raise YoError(
                "limactl edit refused the disk resize (exit %d)" % resize.returncode
            )


LIMA = LimaDriver()


def du_kib(path: str) -> Optional[int]:
    """Host KiB the file allocates; None when there is no such file."""
    try:
        return os.stat(path).st_blocks * STAT_BLOCK_BYTES // KIB
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise YoError("cannot stat %s: %s" % (path, ui.os_error_reason(failure)))
