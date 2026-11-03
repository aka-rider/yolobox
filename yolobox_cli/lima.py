"""The Lima driver: the instance, its config gaps and its lifecycle."""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import aws, proc, state, ui, unlock
from .config import (
    AWS_BROKER_GUEST_SOCK,
    DEFAULT_INSTANCE,
    GIB,
    KIB,
    MAX_PORT,
    MIB,
    OP_GUEST_SOCK,
    SCRIPT_DIR,
    USAGE_EXIT,
)
from .driver import NO_SIZE, Size
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

# The guest TCP ports lima mirrors onto the host's 127.0.0.1: unprivileged and
# below the macOS ephemeral range (49152). A Linux host mirrors nothing: it has no
# fixed range to give away, so `yo port` hands out host ports from
# host_port_window(), the ports above the kernel's ephemeral range.
MIRRORED_TCP_PORTS = [1024, 49151]

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

    def require_tool(self) -> None:
        proc.require_tool("limactl", "brew install lima")

    def op_sock(self) -> str:
        return os.path.join(
            state.home(),
            "Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock",
        )

    def initrd_identity_agent(self) -> str:
        return "none"

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
            else ["--set", ".ssh.localPort=<free port>"]
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
        # Only the default instance is created here, from lima/yolobox.yaml, and
        # at its size. Any other needs a port yo can know, which only its creator
        # can choose, and a size is limactl's to set.
        if size.asked:
            return self.sized_creation_refusal(size)
        if state.box.name == DEFAULT_INSTANCE:
            return None
        return "instance %s does not exist — create it with:\n    %s" % (
            state.box.name,
            self.create_command(NO_SIZE),
        )

    def sized_creation_refusal(self, size: Size) -> str:
        return (
            "yo does not size a lima instance — create %s with:\n    %s\n"
            "then run this command again without %s"
            % (state.box.name, self.create_command(size), " ".join(size.flags()))
        )

    def existing_size_refusal(self, size: Size) -> str:
        ways = []
        if size.memory_gib is not None:
            ways.append(
                "for the memory, stop it (%s), then: limactl edit --yes %s --memory %d"
                % (state.yo_cmd("down"), state.box.name, size.memory_gib)
            )
        if size.disk_gib is not None:
            ways.append(
                "to grow the disk, keeping what is on it: %s (a smaller disk means "
                "deleting the box, and everything on it is lost: %s)"
                % (state.yo_cmd("disk-grow", str(size.disk_gib)), self.delete_command())
            )
        return (
            "%s already exists, and limactl sized it when it was created — drop %s, "
            "or:\n    %s\nthen run this command again without them"
            % (state.box.name, " ".join(size.flags()), "\n    ".join(ways))
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
        return gaps

    def fatal_gaps(self, gaps: Sequence[str]) -> List[str]:
        return [gap for gap in gaps if gap in LIMA_FATAL_GAPS]

    def host_gaps(self) -> List[str]:
        return []

    def config_gap_message(self, gaps: Sequence[str]) -> str:
        missing = [gap for gap in gaps if gap not in LIMA_SETTING_GAPS]
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
        # lima/yolobox.yaml or limactl sizes the instance, never yo: vm_up has
        # refused any size before the passphrase, and this keeps one from
        # being dropped should a caller skip that.
        if size.asked:
            raise YoError(
                self.existing_size_refusal(size)
                if lima_instance_exists()
                else self.sized_creation_refusal(size)
            )
        if lima_instance_exists():
            start_argv = ["limactl", "start", "--yes", state.box.name]
        else:
            start_argv = [
                "limactl",
                "start",
                "--name",
                state.box.name,
                "--yes",
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
        self.limactl_stop(
            [state.box.name],
            "limactl stop hangs on the vz driver sometimes (TODO.md); recover with: "
            "%s, then kill -9 any leftover 'limactl hostagent' for %s, then %s"
            % (
                state.yo_cmd("ssh", "sudo", "systemctl", "reboot"),
                state.box.name,
                state.yo_cmd("up"),
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
            raise YoError("%s\n    %s" % (failure.message, hang_recovery))

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

    def delete_command(self) -> str:
        return "limactl delete %s" % state.box.name

    def list_command(self) -> str:
        return "limactl list"

    def shown_instance_file(self, name: str) -> str:
        """A file of the instance directory, spelled from ~ for a printed hint."""
        return "~/%s/%s/%s" % (self.instances_dir, state.box.name, name)

    def boot_log(self) -> str:
        return self.shown_instance_file("serialv.log")

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

    def show_status(self) -> None:
        proc.run(["limactl", "list", state.box.name], capture=False)

    def disk_image(self) -> Optional[Tuple[str, int]]:
        kib = du_kib(os.path.join(state.instance_dir(), "disk"))
        if kib is None:
            return None
        return self.shown_instance_file("disk"), kib

    def disk_ceiling_gib(self) -> int:
        probe = proc.run(["limactl", "list", state.box.name, "--format", "{{.Disk}}"])
        current_bytes = probe.stdout.strip() if probe.returncode == 0 else ""
        if not proc.is_ascii_number(current_bytes):
            raise YoError(
                "limactl reports no disk size for %s — does the instance exist? (%s)"
                % (state.box.name, state.yo_cmd("status"))
            )
        return int(current_bytes) // GIB

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

        self.stop()
        resize = proc.run(
            ["limactl", "edit", "--yes", state.box.name, "--disk", str(wanted_gib)],
            capture=False,
        )
        if resize.returncode != 0:
            raise YoError(
                "limactl refused the resize and %s is left stopped — start it again with: %s"
                % (state.box.name, state.yo_cmd("up"))
            )


LIMA = LimaDriver()


def du_kib(path: str) -> Optional[int]:
    """Host KiB the file allocates; None when there is no such file."""
    try:
        return os.stat(path).st_blocks * STAT_BLOCK_BYTES // KIB
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise YoError("cannot stat %s: %s" % (path, failure.strerror))
