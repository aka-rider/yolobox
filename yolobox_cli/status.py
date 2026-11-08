"""yo status: the per-component checks and the exit code."""

from __future__ import annotations

import argparse
import shutil
from typing import Callable, List, Optional, Tuple, TypeVar

from . import aws, config, forwards, guest, proc, release, sshx, state, ui, unlock, vm
from .config import EGRESS_UNIT, LUKS_DEVICE, MIB, PROXY_SOCKET_UNITS
from .sshx import OPERATOR
from .ui import Report, StatusLine, YoError
from .unlock import BootStage

Read = TypeVar("Read")


def driver_lines(
    what: str, read: Callable[[], Read], lines_of: Callable[[Read], List[StatusLine]]
) -> List[StatusLine]:
    """The report's lines from one read of the host driver: what `lines_of`
    makes of the value, or one warning naming `what` could not be read. Every
    driver read `yo status` makes goes through here, so a host tool that fails
    (virsh, getfacl, limactl) costs the report one line, never the rest of it."""
    try:
        value = read()
    except YoError as failure:
        return [
            StatusLine(
                "%s: WARNING cannot read %s: %s"
                % (state.driver().name, what, failure.message),
                True,
            )
        ]
    return lines_of(value)


def guest_field(text: str, key: str) -> str:
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return fields.get(key, "")


class GuestStatus:
    """The KEY=value block `yolobox-guest status` printed. Every read records a
    key the block lacks, or answers outside the words asked for, in `problems`,
    and `yo status` warns once per entry."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.problems: List[str] = []

    def value(self, key: str) -> str:
        """The key's value as one printable line; "" (and a problem) when missing."""
        value = guest_field(self.text, key)
        if not value:
            self.problems.append("%s not reported" % key)
        return ui.shown(value)

    def word(self, key: str, *words: str) -> str:
        """The key's value when it is one of `words`, else "" and a problem."""
        value = guest_field(self.text, key)
        if value in words:
            return value
        if value:
            self.problems.append(
                "%s answered '%s', not %s" % (key, ui.shown(value), " or ".join(words))
            )
        else:
            self.problems.append("%s not reported" % key)
        return ""


def tailscale_line(backend: str) -> StatusLine:
    if backend == "NeedsLogin":
        return StatusLine(
            "tailscale: not logged in (optional) — to join a tailnet: %s"
            % state.yo_cmd("ssh", "sudo", "tailscale", "up")
        )
    if backend == "NeedsMachineAuth":
        return StatusLine(
            "tailscale: awaiting approval in the tailnet admin console (optional)"
        )
    if backend != "Running":
        return StatusLine(
            "tailscale: WARNING %s — fix with: %s"
            % (backend, state.yo_cmd("ssh", "sudo", "tailscale", "up")),
            True,
        )
    return StatusLine("tailscale: Running")


# The unit that loads the egress table. It has no ExecStop, so any state but
# active means the table was never loaded this boot, or a switch failed to.
def egress_line(active_state: str) -> StatusLine:
    if active_state == "active":
        return StatusLine("egress: active")
    return StatusLine(
        "egress: WARNING %s.service %s" % (EGRESS_UNIT, active_state), True
    )


def proxy_line(unit: str, active_state: str) -> StatusLine:
    return StatusLine(
        "proxy: %s is %s" % (unit, active_state), active_state != "active"
    )


def fstype_line(fstype: str) -> StatusLine:
    if fstype == vm.DISK_FSTYPE_LUKS:
        return StatusLine("disk: %s is %s" % (LUKS_DEVICE, fstype))
    return StatusLine(
        "disk: %s is %s — NOT encrypted; rebuild and reboot: %s"
        % (LUKS_DEVICE, fstype, state.yo_cmd("bootstrap")),
        True,
    )


def network_line(
    internet_ok: bool, gateway_ok: bool, rootdisk_full: bool
) -> StatusLine:
    if rootdisk_full:
        # A full root disk leaves dhcpcd unable to write resolv.conf, which
        # looks like a dead network or a VPN; name the real cause instead.
        return StatusLine(
            "network: WARNING the guest's root disk is full, so name resolution "
            "can fail (dhcpcd cannot write resolv.conf); the probes saw egress %s "
            "and the gateway %s — free space with `%s`"
            % (
                "ok" if internet_ok else "dead",
                "ok" if gateway_ok else "dead",
                state.yo_cmd("gc", "--yes"),
            ),
            True,
        )
    if internet_ok and gateway_ok:
        return StatusLine("network: ok")
    if gateway_ok:
        return StatusLine(
            "network: WARNING guest egress is dead while the gateway answers DNS — "
            "a VPN on the host likely owns its default route",
            True,
        )
    if internet_ok:
        return StatusLine(
            "network: WARNING the gateway does not answer DNS while egress works — "
            "DNS through the gateway will fail; check the host's VPN or DNS filter",
            True,
        )
    return StatusLine(
        "network: WARNING the guest reaches neither the internet nor the gateway's DNS — "
        "check %s's %s line and the host's VPN"
        % (state.yo_cmd("status"), state.driver().name),
        True,
    )


def guest_lines(status: GuestStatus) -> List[StatusLine]:
    """Every line made from the guest's block, then one warning per key it
    lacked or answered badly."""
    lines: List[StatusLine] = []
    for unit in PROXY_SOCKET_UNITS:
        active_state = status.value(unit)
        if active_state:
            lines.append(proxy_line(unit, active_state))
    for key, line_of in (
        ("tailscale", tailscale_line),
        ("egress", egress_line),
        ("fstype", fstype_line),
    ):
        value = status.value(key)
        if value:
            lines.append(line_of(value))
    internet = status.word("internet", "ok", "dead")
    gateway = status.word("gateway_dns", "ok", "dead")
    rootdisk = status.word("rootdisk", "ok", "full")
    if internet and gateway and rootdisk:
        lines.append(
            network_line(internet == "ok", gateway == "ok", rootdisk == "full")
        )
    rootfs = status.value("rootfs")
    if rootfs:
        lines.append(StatusLine("disk: guest %s" % rootfs))
    if status.word("rootfs_full", "yes", "no") == "yes":
        lines.append(
            StatusLine(
                "disk: WARNING guest / is nearly full — reclaim with: %s, "
                "or raise the ceiling with: %s"
                % (
                    state.yo_cmd("gc", "--deep", "--yes"),
                    state.yo_cmd("disk-grow", "<GiB>"),
                ),
                True,
            )
        )
    lines += [
        StatusLine("status: WARNING %s" % problem, True) for problem in status.problems
    ]
    return lines


def status_guest(report: Report) -> None:
    """The guest's side of the report, from one `yolobox-guest status` as the
    operator and the agent's probe of the 1Password forward."""
    completed = guest.guest_helper(OPERATOR, "status")
    if completed.returncode != 0:
        # The seed image has no helper; a box built by another yo may lack the
        # subcommand. Either way the one remedy is a bootstrap.
        report.warn(
            "yolobox: host yo %s, box built by nothing readable — %s; run %s"
            % (
                config.YO_VERSION,
                proc.failed_message(
                    "%s status" % config.HELPER,
                    completed.returncode,
                    proc.failure_detail(completed),
                    "on the box",
                ),
                state.yo_cmd("bootstrap"),
            )
        )
        return
    status = GuestStatus(completed.stdout)
    guest_version = status.value("version")
    ui.out(
        "yolobox: host yo %s, box built by %s from %s"
        % (
            config.YO_VERSION,
            guest_version or "nothing readable",
            status.value("flake_ref") or "an unreadable ref",
        )
    )
    if guest_version:
        drift = release.version_drift(config.YO_VERSION, guest_version)
        if drift:
            report.warn(drift)

    op_alive = forwards.forward_is_up(forwards.one_password_forward())
    report.emit(
        StatusLine(
            "1password: guest forward is %s" % ("up" if op_alive else "down"),
            not op_alive,
        )
    )
    for line in guest_lines(status):
        report.emit(line)


def disk_image_lines(image: Optional[Tuple[str, int]]) -> List[StatusLine]:
    if image is None:
        return []
    label, image_kib = image
    return [
        StatusLine(
            "disk: image %s allocates %dGiB on this host" % (label, image_kib // MIB)
        )
    ]


def aws_lines(vm_reachable: bool) -> List[StatusLine]:
    disabled = StatusLine(
        "aws: disabled (no 'aws' CLI or no SSO profiles in %s)"
        % state.tilde(aws.aws_config_path())
    )
    if shutil.which("aws") is None:
        return [disabled]
    try:
        allowed = aws.aws_allowlist()
    except YoError as failure:
        return [StatusLine("aws: config unreadable: %s" % failure.message, True)]
    if not allowed:
        return [disabled]
    if not vm_reachable:
        return [StatusLine("aws: guest forward not checked (VM unreachable)")]

    forward_up = forwards.forward_is_up(forwards.aws_broker_forward())
    lines = [
        StatusLine(
            "aws: guest forward is %s" % ("up" if forward_up else "down"),
            not forward_up,
        )
    ]
    return lines + driver_lines(
        "the pid the broker watches",
        state.driver().keepalive_pid,
        broker_lines,
    )


def broker_lines(pid: Optional[int]) -> List[StatusLine]:
    """The broker's line against `pid`, the one it should be watching (None: the
    host has no such process now)."""
    keepalive = state.driver().hints.keepalive_name
    health = aws.aws_broker_health()
    if health is None:
        return [
            StatusLine(
                "aws: broker not reachable on this host's %s"
                % aws.aws_broker_ctl_sock(),
                True,
            )
        ]
    watched = health.get("watch_pid")
    if pid is None:
        return [
            StatusLine(
                "aws: broker running but %s has no %s pid now (it watches pid %s)"
                % (state.box.name, keepalive, watched),
                True,
            )
        ]
    if aws.checked_pid(watched) == pid:
        return [
            StatusLine("aws: broker healthy (watching %s pid %s)" % (keepalive, pid))
        ]
    return [
        StatusLine(
            "aws: broker running but stale (watching pid %s, current %s pid %s)"
            % (watched, keepalive, pid),
            True,
        )
    ]


def initrd_disk_line(ask_pending: bool) -> str:
    """The `disk:` line of a box that answers in its initrd."""
    if ask_pending:
        return "disk: locked — %s is in its initrd, waiting for the passphrase; %s" % (
            state.box.name,
            state.yo_cmd("up"),
        )
    return (
        "disk: locked — %s is in its initrd with no passphrase ask pending: "
        "encrypting its disk in place, or stuck; do not stop it, finish the boot "
        "with: %s" % (state.box.name, state.yo_cmd("up"))
    )


def cmd_status(args: argparse.Namespace) -> None:
    report = Report()
    listing_failure = state.driver().show_status()
    if listing_failure is not None:
        report.warn(listing_failure)

    # Every guest probe below fails the same way on a VM that is down, so ask
    # once and say it once instead of printing a warning per probe. The stage
    # is read off the banner and the unpinned channel: a box locked in its
    # initrd answers with a per-boot host key that a pinned probe would warn about.
    stage = unlock.boot_stage()
    vm_reachable = stage is BootStage.STAGE2
    if not vm_reachable:
        if stage is BootStage.UNKNOWN:
            report.warn("vm: %s" % unlock.unknown_stage_message())
        else:
            report.warn(
                "vm: unreachable — is it up? start it with: %s" % state.yo_cmd("up")
            )
        if stage is BootStage.INITRD:
            ui.out(initrd_disk_line(unlock.initrd_ask_pending()))

    if vm_reachable:
        try:
            agent_problem = sshx.agent_login_problem()
            if agent_problem is not None:
                report.warn("agent: login fails — %s" % agent_problem)
            status_guest(report)
        except sshx.Unreachable as lost:
            vm_reachable = False
            report.warn("vm: lost while probing — %s" % lost.message)

    for line in driver_lines(
        "the disk image's size", state.driver().disk_image, disk_image_lines
    ):
        report.emit(line)

    for line in aws_lines(vm_reachable):
        report.emit(line)

    if report.warnings:
        raise YoError(None, code=1)
