"""yo status: the per-component checks and the exit code."""

from __future__ import annotations

import argparse
import shutil
from typing import Callable, List, Optional, Tuple, TypeVar

from . import (
    aws,
    common,
    config,
    forwards,
    guest,
    proc,
    release,
    sshx,
    state,
    ui,
    unlock,
    vm,
)
from .config import (
    EGRESS_UNIT,
    LUKS_DEVICE,
    MIB,
    NETWORK_READY_UNIT,
    PROXY_SOCKET_UNITS,
)
from .sshx import OPERATOR
from .ui import Report, StatusLine, YoError
from .unlock import BootStage, Endpoint

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


# The unit that drops the network-ready flag fails when the DHCP lease brought
# no nameserver; its journal holds the lease it saw.
def network_ready_line(active_state: str) -> StatusLine:
    if active_state == "active":
        return StatusLine("network-ready: active")
    return StatusLine(
        "network-ready: WARNING %s.service %s, so the guest may resolve no name; "
        "its journal says why: %s"
        % (
            NETWORK_READY_UNIT,
            active_state,
            state.yo_cmd("ssh", "journalctl", "-u", NETWORK_READY_UNIT),
        ),
        True,
    )


def proxy_line(unit: str, active_state: str) -> StatusLine:
    """Nothing starts a proxy socket at boot: `yo` does, once the forward it
    proxies exists, so a box rebooted since its last `yo` command has them
    inactive."""
    if active_state == "inactive":
        return StatusLine(
            "proxy: %s is inactive — start it with: %s" % (unit, state.yo_cmd("up")),
            True,
        )
    return StatusLine(
        "proxy: %s is %s" % (unit, active_state), active_state != "active"
    )


def fstype_line(fstype: str) -> StatusLine:
    if fstype == vm.DISK_FSTYPE_LUKS:
        return StatusLine("disk: %s is %s" % (LUKS_DEVICE, fstype))
    if fstype == vm.DISK_FSTYPE_PLAIN:
        return StatusLine(
            "disk: %s is %s — NOT encrypted; rebuild and reboot: %s"
            % (LUKS_DEVICE, fstype, state.yo_cmd("bootstrap")),
            True,
        )
    return StatusLine(
        "disk: %s is '%s', neither %s nor %s"
        % (LUKS_DEVICE, ui.shown(fstype), vm.DISK_FSTYPE_LUKS, vm.DISK_FSTYPE_PLAIN),
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
            "network: WARNING guest egress is dead while the gateway resolves "
            "names — "
            "a VPN on the host likely owns its default route",
            True,
        )
    if internet_ok:
        return StatusLine(
            "network: WARNING the gateway resolves no name while egress works — "
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
        ("network_ready", network_ready_line),
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


def forward_line(name: str, forward: forwards.ReverseForward) -> StatusLine:
    """How a guest forward stands: up, or down with what its probe said and the
    command that declares it again."""
    failure = forwards.forward_failure(forward)
    if failure is None:
        return StatusLine("%s: guest forward is up" % name)
    return StatusLine(
        "%s: guest forward is down (%s) — restore it with: %s"
        % (name, failure, state.yo_cmd("up")),
        True,
    )


def status_guest(report: Report) -> None:
    """The guest's side of the report, from one `yolobox-guest status` as the
    operator and the agent's probe of the 1Password forward. The probe is
    made first and printed after the helper's own header: a box whose helper
    fails still says whether its forward is up."""
    forward = forward_line("1password", forwards.one_password_forward())
    completed = guest.guest_helper(OPERATOR, "status")
    if completed.returncode != 0:
        # A bootstrapped box whose helper fails: a box built by another yo may
        # lack the subcommand, and the one remedy is a bootstrap.
        report.warn(
            "yolobox: %s; run %s"
            % (
                proc.failed_message(
                    "%s status" % config.HELPER,
                    completed.returncode,
                    proc.failure_detail(completed),
                    "on the box",
                ),
                state.yo_cmd("bootstrap"),
            )
        )
        report.emit(forward)
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

    report.emit(forward)
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


def aws_lines(unchecked: Optional[str]) -> List[StatusLine]:
    """The aws lines; `unchecked` says why the guest forward is not probed
    (None: it is)."""
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
    if unchecked is not None:
        return [StatusLine("aws: guest forward not checked (%s)" % unchecked)]

    return [forward_line("aws", forwards.aws_broker_forward())] + driver_lines(
        "the pid the broker watches",
        state.driver().keepalive_pid,
        broker_lines,
    )


def broker_lines(pid: Optional[int]) -> List[StatusLine]:
    """The broker's line against `pid`, the one it should be watching (None: the
    host has no such process now)."""
    keepalive = state.driver().hints.keepalive_name
    try:
        health = aws.aws_broker_health()
    except YoError as failure:
        return [StatusLine("aws: broker unhealthy: %s" % failure.message, True)]
    if health is None:
        return [
            StatusLine(
                "aws: no broker listens on this host's %s"
                % state.aws_broker_ctl_sock(),
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


def status_boot_stage(report: Report) -> bool:
    """Report which sshd answers, and whether the VM is reachable as the
    operator. The stage is read off the banner and the unpinned channel: a box
    locked in its initrd answers with a per-boot host key that a pinned probe
    would warn about. Root is asked only under the box lock, so a box in its
    initrd is looked inside only while no other yo acts on it."""
    endpoint = state.driver().ssh_facts()
    banner = unlock.endpoint_kind(endpoint)
    if banner.kind is Endpoint.UNKNOWN:
        report.warn("vm: %s" % unlock.unknown_banner_error(banner).message)
        return False
    if banner.kind is not Endpoint.INITRD:
        return report_stage(report, unlock.operator_stage(endpoint, banner))
    try:
        with state.box_lock(wait=False) as held:
            stage = unlock.boot_stage(held)
            reachable = report_stage(report, stage)
            if stage.kind is BootStage.INITRD:
                gate = unlock.require_initrd(held)
                ui.out(initrd_disk_line(unlock.initrd_ask_pending(gate)))
            return reachable
    except common.LockBusy:
        report.warn(
            "vm: %s is in its initrd, and another yo is acting on it (an up may "
            "be unlocking it), so yo does not look inside; run %s again when "
            "that command is done" % (state.box.name, state.yo_cmd("status"))
        )
        return False


def report_stage(report: Report, stage: unlock.Stage) -> bool:
    """Whether the VM is reachable as the operator; a warning when it is not."""
    if stage.kind is BootStage.STAGE2:
        return True
    if stage.kind is BootStage.UNKNOWN:
        report.warn("vm: %s" % unlock.unknown_stage_message(stage))
    else:
        report.warn(
            "vm: unreachable%s — is it up? start it with: %s"
            % (unlock.stage_said(stage), state.yo_cmd("up"))
        )
    return False


def cmd_status(args: argparse.Namespace) -> None:
    report = Report()
    listing_failure = state.driver().show_status()
    if listing_failure is not None:
        report.warn(listing_failure)

    # Every guest probe below fails the same way on a VM that is down, so ask
    # once and say it once instead of printing a warning per probe.
    vm_reachable = status_boot_stage(report)
    aws_unchecked: Optional[str] = None if vm_reachable else "VM unreachable"
    if vm_reachable:
        try:
            if sshx.bootstrapped(mux=False):
                agent_problem = sshx.broken_agent_login()
                if agent_problem is not None:
                    report.warn("agent: login fails — %s" % agent_problem)
                status_guest(report)
            else:
                report.warn("yolobox: %s" % sshx.not_bootstrapped_message())
                aws_unchecked = "not bootstrapped"
        except sshx.Unreachable as lost:
            aws_unchecked = "VM unreachable"
            report.warn("vm: lost while probing — %s" % lost.message)

    for line in driver_lines(
        "the disk image's size", state.driver().disk_image, disk_image_lines
    ):
        report.emit(line)

    for line in aws_lines(aws_unchecked):
        report.emit(line)

    if report.warnings:
        raise YoError(None, code=1)
