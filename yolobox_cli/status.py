"""yo status: the per-component checks and the exit code."""

from __future__ import annotations

import argparse
import enum
import json
import shlex
import shutil
import subprocess
from typing import Callable, List, NamedTuple, Optional

from . import aws, config, forwards, guest, proc, release, sshx, state, ui, unlock, vm
from .config import LUKS_DEVICE, MIB, PROXY_SOCKET_UNITS
from .sshx import OPERATOR
from .ui import Report, StatusLine, YoError
from .unlock import BootStage
from .vm import DiskState


# `yo status` warns when the guest's / is this full.
DISK_WARN_PERCENT = 90

# One operator round trip: tailscale's own status JSON. `command -v` first, so
# a box predating nix/tailscale.nix answers with a marker instead of a bare
# "not found".
TAILSCALE_PROBE = (
    "command -v tailscale >/dev/null 2>&1 || { echo %s; exit 0; }; "
    "tailscale status --json"
) % shlex.quote("NO_TAILSCALE")


def guest_field(text: str, key: str) -> str:
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return fields.get(key, "")


def tailscale_status_line(stdout: str, stderr: str) -> StatusLine:
    if stdout.strip() == "NO_TAILSCALE":
        return StatusLine(
            "tailscale: box predates the tailnet module; run `%s`"
            % state.yo_cmd("bootstrap")
        )
    try:
        backend = json.loads(stdout)["BackendState"]
    except (ValueError, KeyError, TypeError):
        detail = stderr.strip() or stdout.strip() or "no output"
        return StatusLine("tailscale: WARNING status unreadable: %s" % detail, True)
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


# nix/egress.nix: the table the guest's egress rules live in, and the unit that
# loads it. The unit is "active" only when the full rules loaded; a loaded unit
# in any other state means the box is in recovery mode (nix/egress.md). A box
# built before the unit existed reports LoadState not-found, and systemctl show
# exits 0 for it, so one round trip tells the two apart. A masked unit never
# loads the table at boot, so it gets its own warning.
EGRESS_TABLE = "inet yolobox-egress"
EGRESS_UNIT = "yolobox-egress"
EGRESS_PROBE = [
    "systemctl",
    "show",
    "-p",
    "LoadState",
    "-p",
    "ActiveState",
    EGRESS_UNIT,
]
# The live table, read over the operator channel. A flushed, replaced or emptied
# table leaves the unit "active", so the unit state alone cannot vouch for it.
# -n: sudo must never wait for a password on a channel with no terminal.
EGRESS_TABLE_PROBE = ["sudo", "-n", "nft", "list", "table"] + EGRESS_TABLE.split()
NFT_MISSING_TABLE = "No such file or directory"
EGRESS_PRIVATE_V4 = "10.0.0.0/8"
EGRESS_PRIVATE_V6 = "fc00::/7"


def egress_table_holds(listing: str) -> bool:
    # Structural invariants only. lanAllow and tunnelInterfaces boxes add
    # accepts and a postrouting chain, so comparing rule content would raise
    # a false alarm on exactly those boxes.
    lines = [line.strip() for line in listing.splitlines()]
    has_output_hook = any(
        line.startswith("type filter hook output") for line in lines
    ) and any(line.startswith("chain output") for line in lines)
    has_local_accept = any(
        line.startswith("oifname {")
        and line.endswith("} accept")
        and '"lo"' in line
        and '"tailscale0"' in line
        for line in lines
    )
    rejects_v4 = any(
        "ip daddr" in line and EGRESS_PRIVATE_V4 in line and "reject" in line
        for line in lines
    )
    rejects_v6 = any(
        "ip6 daddr" in line and EGRESS_PRIVATE_V6 in line and "reject" in line
        for line in lines
    )
    return has_output_hook and has_local_accept and rejects_v4 and rejects_v6


def egress_active_line(table: subprocess.CompletedProcess) -> StatusLine:
    # The unit says active; ask the kernel. A probe that failed for any reason
    # other than "no such table" (ssh, sudo) says nothing about the table, so
    # the unit state stands.
    blocked = StatusLine("egress: host and LAN blocked")
    if table.returncode != 0:
        if NFT_MISSING_TABLE not in table.stderr:
            return blocked
    elif egress_table_holds(table.stdout):
        return blocked
    return StatusLine(
        "egress: WARNING unit active but table looks wrong — the box may reach "
        "the host; restart it with: %s, or run %s"
        % (
            state.yo_cmd("ssh", "sudo", "systemctl", "restart", EGRESS_UNIT),
            state.yo_cmd("bootstrap"),
        ),
        True,
    )


class EgressTier(enum.Enum):
    """Which fallback table a box whose egress unit is not active is running on."""

    RECOVERY = "recovery"
    LAST_RESORT = "last-resort"
    NONE = "none"  # no table in the kernel: the box fails open
    UNREADABLE = "unreadable"  # the probe itself failed
    UNKNOWN = "unknown"  # a table that is none of the tiers


# The chain name says which tier loaded (nix/egress.nix). Compared as whole
# lines, never as prefixes, because "chain output" begins all three names.
# Plain "output" is the full table's name and also what a box built before the
# tiers existed called its recovery table, so under an inactive unit it is a
# recovery table.
EGRESS_TIER_CHAINS = {
    "chain output_lastresort {": EgressTier.LAST_RESORT,
    "chain output_recovery {": EgressTier.RECOVERY,
    "chain output {": EgressTier.RECOVERY,
}


def egress_tier(table: subprocess.CompletedProcess) -> EgressTier:
    if table.returncode != 0:
        if NFT_MISSING_TABLE in table.stderr:
            return EgressTier.NONE
        return EgressTier.UNREADABLE
    chains = {
        EGRESS_TIER_CHAINS[line]
        for line in (raw.strip() for raw in table.stdout.splitlines())
        if line in EGRESS_TIER_CHAINS
    }
    for tier in (EgressTier.LAST_RESORT, EgressTier.RECOVERY):
        if tier in chains:
            return tier
    return EgressTier.UNKNOWN


def egress_fallback_line(
    active_state: str, table: subprocess.CompletedProcess
) -> StatusLine:
    tier = egress_tier(table)
    unit = "%s %s" % (EGRESS_UNIT, active_state)
    if tier is EgressTier.RECOVERY:
        what = (
            "recovery table — the egress rules failed to load (%s), "
            "the box has no network" % unit
        )
    elif tier is EgressTier.LAST_RESORT:
        what = (
            "last-resort table — neither the egress rules nor the recovery rules "
            "loaded (%s), the box has loopback, established and related flows and DHCP only"
            % unit
        )
    elif tier is EgressTier.NONE:
        what = (
            "no table (fail-open) — no egress rules are loaded (%s), "
            "the box may reach the host and the LAN" % unit
        )
    elif tier is EgressTier.UNREADABLE:
        what = (
            "the egress rules failed to load (%s) and the live table could not be "
            "read: %s" % (unit, table.stderr.strip() or "no message")
        )
    else:
        what = (
            "the egress rules failed to load (%s) and the live table is none of "
            "the three tiers" % unit
        )
    return StatusLine(
        "egress: WARNING %s; roll back with: %s; logs: %s; table: %s"
        % (
            what,
            state.yo_cmd("ssh", "sudo", "nixos-rebuild", "switch", "--rollback"),
            state.yo_cmd("ssh", "journalctl", "-u", EGRESS_UNIT),
            state.yo_cmd("ssh", "sudo", "nft", "list", "table", EGRESS_TABLE),
        ),
        True,
    )


def egress_status_line(
    probe: subprocess.CompletedProcess,
    read_table: Callable[[], subprocess.CompletedProcess],
) -> StatusLine:
    load_state = guest_field(probe.stdout, "LoadState")
    active_state = guest_field(probe.stdout, "ActiveState")
    if load_state == "not-found":
        return StatusLine(
            "egress: box predates the egress table; run %s" % state.yo_cmd("bootstrap"),
            True,
        )
    if load_state == "masked":
        return StatusLine(
            "egress: WARNING the egress table is masked — the box may reach the host; "
            "unmask with: %s"
            % state.yo_cmd("ssh", "sudo", "systemctl", "unmask", EGRESS_UNIT),
            True,
        )
    if load_state != "loaded" or not active_state:
        stderr = probe.stderr.strip()
        return StatusLine(
            "egress: not checked — the probe answered %r (exit %d)%s"
            % (
                probe.stdout.strip(),
                probe.returncode,
                ": " + stderr if stderr else "",
            )
        )
    if active_state == "active":
        return egress_active_line(read_table())
    return egress_fallback_line(active_state, read_table())


class Gateway(enum.Enum):
    # net-check's word for the probe of lima's gateway. SKIPPED means the box
    # blocks lima's gateway by design: /etc/yolobox/gateway-blocked exists.
    OK = "ok"
    DEAD = "dead"
    SKIPPED = "skipped"


GATEWAY_WORDS = {gateway.value: gateway for gateway in Gateway}


class RootDisk(enum.Enum):
    # net-check's word for the root disk. NOT_REPORTED is a box built before
    # the field existed: nothing is known about its disk, which is not "ok".
    OK = "ok"
    FULL = "full"
    NOT_REPORTED = ""


ROOT_DISK_WORDS = {root_disk.value: root_disk for root_disk in RootDisk}


class NetCheck(NamedTuple):
    egress_ok: bool
    gateway: Gateway
    root_disk: RootDisk


def network_status_line(check: NetCheck) -> StatusLine:
    egress_ok, gateway, root_disk = check
    if root_disk is RootDisk.FULL:
        # A full root disk leaves dhcpcd unable to write resolv.conf, which
        # looks like a dead network or a VPN; name the real cause instead.
        return StatusLine(
            "network: WARNING the guest's root disk is full, so name resolution "
            "can fail (dhcpcd cannot write resolv.conf); the probes saw egress %s "
            "and lima's gateway %s — free space with `%s`; see docs/disk.md"
            % (
                "ok" if egress_ok else "dead",
                gateway.value,
                state.yo_cmd("gc", "--yes"),
            ),
            True,
        )
    if gateway is Gateway.SKIPPED:
        if egress_ok:
            return StatusLine(
                "network: ok (lima's gateway probe skipped: this box blocks it by design)"
            )
        return StatusLine(
            "network: WARNING guest egress is dead "
            "(lima's gateway probe skipped: this box blocks it by design)",
            True,
        )
    if egress_ok and gateway is Gateway.OK:
        return StatusLine("network: ok")
    if gateway is Gateway.OK:
        return StatusLine(
            "network: WARNING guest egress is dead while lima's gateway answers DNS — "
            "a VPN on the host likely owns its default route",
            True,
        )
    if egress_ok:
        return StatusLine(
            "network: WARNING lima's gateway does not answer DNS while egress works — "
            "DNS through the gateway will fail; check the host's VPN or DNS filter",
            True,
        )
    return StatusLine(
        "network: WARNING the guest reaches neither the internet nor lima's gateway DNS — "
        "check %s's %s line and the host's VPN"
        % (state.yo_cmd("status"), state.driver().name),
        True,
    )


def guest_network_line() -> StatusLine:
    try:
        completed = guest.guest_helper(OPERATOR, "net-check", [])
    except sshx.Unreachable:
        raise
    except YoError as e:
        return StatusLine("network: not checked — %s" % e.message)
    check = parse_net_check(completed.stdout)
    if check is None:
        stderr = completed.stderr.strip()
        return StatusLine(
            "network: not checked — net-check answered %r (exit %d)%s"
            % (
                completed.stdout.strip(),
                completed.returncode,
                ": " + stderr if stderr else "",
            )
        )
    return network_status_line(check)


def parse_net_check(text: str) -> Optional[NetCheck]:
    egress = guest_field(text, "egress")
    gateway = guest_field(text, "gateway")
    if egress not in ("ok", "dead") or gateway not in GATEWAY_WORDS:
        return None
    rootdisk = guest_field(text, "rootdisk")
    if rootdisk not in ROOT_DISK_WORDS:
        return None
    return NetCheck(egress == "ok", GATEWAY_WORDS[gateway], ROOT_DISK_WORDS[rootdisk])


def status_guest_services(report: Report) -> None:
    guest_version = release.built_by()
    unknown_note = "unknown (box built before /etc/yolobox/version existed)"
    ui.out(
        "yolobox: host yo %s, box built by %s"
        % (config.YO_VERSION, guest_version or unknown_note)
    )
    drift = release.version_drift(config.YO_VERSION, guest_version)
    if drift:
        report.warn(drift)

    if guest.vm_units_present():
        op_alive = forwards.forward_is_up(forwards.one_password_forward())
        report.emit(
            StatusLine(
                "1password: guest forward is %s" % ("up" if op_alive else "down"),
                not op_alive,
            )
        )

        for unit in PROXY_SOCKET_UNITS:
            unit_state = sshx.ssh_run(
                OPERATOR,
                ["systemctl", "show", "-p", "ActiveState", "--value", unit],
                check=False,
            ).stdout.strip()
            report.emit(
                StatusLine(
                    "proxy: %s is %s" % (unit, unit_state or "unknown"),
                    unit_state != "active",
                )
            )
    else:
        report.warn("yo: %s" % forwards.units_missing_note())

    tailscale_probe = sshx.ssh_run(OPERATOR, ["sh", "-c", TAILSCALE_PROBE], check=False)
    report.emit(tailscale_status_line(tailscale_probe.stdout, tailscale_probe.stderr))

    report.emit(
        egress_status_line(
            sshx.ssh_run(OPERATOR, EGRESS_PROBE, check=False),
            lambda: sshx.ssh_run(OPERATOR, EGRESS_TABLE_PROBE, check=False),
        )
    )

    report.emit(guest_network_line())


def encryption_line() -> StatusLine:
    try:
        fstype = vm.luks_device_fstype()
    except sshx.Unreachable as lost:
        return StatusLine("disk: %s" % lost.message, True)
    except YoError as failed:
        return StatusLine(
            "disk: cannot read the type of %s: %s" % (LUKS_DEVICE, failed.message)
        )
    disk_state = vm.classify_disk(fstype)
    if disk_state is DiskState.UNREADABLE:
        return StatusLine("disk: cannot read the type of %s" % LUKS_DEVICE)
    if disk_state is DiskState.ENCRYPTED:
        return StatusLine("disk: %s is %s" % (LUKS_DEVICE, fstype))
    return StatusLine(
        "disk: %s is %s — NOT encrypted; rebuild and reboot: %s"
        % (LUKS_DEVICE, fstype, state.yo_cmd("bootstrap")),
        True,
    )


def disk_lines(vm_reachable: bool) -> List[StatusLine]:
    """The root disk's lines: usage, encryption and the host's image."""
    lines: List[StatusLine] = []
    guest_df = ""
    if vm_reachable:
        try:
            guest_df = guest.guest_root_usage().human_line
        except YoError as error:
            lines.append(StatusLine("disk: guest %s" % error.message))
    if guest_df:
        lines.append(StatusLine("disk: guest %s" % guest_df))
        fields = guest_df.split()
        use_pct = fields[4].rstrip("%") if len(fields) > 4 else ""
        if proc.is_ascii_number(use_pct) and int(use_pct) >= DISK_WARN_PERCENT:
            lines.append(
                StatusLine(
                    "disk: WARNING guest / is %s%% full — reclaim with: %s, "
                    "or raise the ceiling with: %s"
                    % (
                        use_pct,
                        state.yo_cmd("gc", "--deep", "--yes"),
                        state.yo_cmd("disk-grow", "<GiB>"),
                    ),
                    True,
                )
            )
    if vm_reachable:
        lines.append(encryption_line())
    image = state.driver().disk_image()
    if image is not None:
        label, image_kib = image
        lines.append(
            StatusLine(
                "disk: image %s allocates %dGiB on this host"
                % (label, image_kib // MIB)
            )
        )
    return lines


def host_gap_line(gap: str) -> StatusLine:
    return StatusLine("%s: WARNING %s" % (state.driver().name, gap), True)


def aws_lines(vm_reachable: bool) -> List[StatusLine]:
    disabled = StatusLine(
        "aws: disabled (no 'aws' CLI or no SSO profiles in ~/.aws/config)"
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
    pid = state.driver().keepalive_pid()
    health = aws.aws_broker_health()
    if health.state is aws.BrokerState.UNREACHABLE:
        lines.append(
            StatusLine(
                "aws: broker not reachable on this host's %s" % aws.aws_broker_sock(),
                True,
            )
        )
    elif pid is not None and aws.checked_pid(health.info.get("watch_pid")) == pid:
        lines.append(
            StatusLine("aws: broker healthy (watching hostagent pid %s)" % pid)
        )
    else:
        lines.append(
            StatusLine(
                "aws: broker running but stale (watching pid %s, current hostagent pid %s)"
                % (health.info.get("watch_pid"), pid),
                True,
            )
        )
    return lines


def initrd_disk_line(reading: unlock.InitrdReading) -> str:
    """The `disk:` line of a box that answers in its initrd, one line per state."""
    initrd = unlock.InitrdState
    if reading.state is initrd.WAITING_FOR_PASSPHRASE:
        advice = "unlock it with: %s" % state.yo_cmd("up")
    elif reading.state is initrd.MIGRATING:
        advice = "do not stop it; finish the boot with: %s" % state.yo_cmd("up")
    else:
        advice = "%s explains how to stop it" % state.yo_cmd("down")
        if reading.cause:
            advice = "%s; %s" % (reading.cause, advice)
    return "disk: locked — %s is in its initrd, %s; %s" % (
        state.box.name,
        reading.state.value,
        advice,
    )


def cmd_status(args: argparse.Namespace) -> None:
    report = Report()
    state.driver().show_status()
    # Said first and once: with no login key every probe below fails, and the
    # lines that need no login (the VM's state, the config gaps) still follow.
    login = vm.login_problem()
    if login is not None:
        report.warn("login: %s" % login)

    # Every guest probe below fails the same way on a VM that is down, so ask
    # once and say it once instead of printing a warning per probe.
    # A box locked in its initrd answers with a per-boot host key that the pinned
    # probes in account_status() would warn about, so tell the stages apart first.
    stage = unlock.boot_stage()
    if stage in (BootStage.INITRD, BootStage.UNKNOWN):
        # No pinned probe against a box that is, or may be, in its initrd.
        status, detail = "unreachable", ""
    else:
        status, detail = sshx.account_status()
    vm_reachable = status != "unreachable"
    if not vm_reachable:
        if detail:
            ui.err(detail)
        if stage is BootStage.UNKNOWN:
            report.warn("vm: %s" % vm.unknown_stage_message(login))
        else:
            report.warn(
                "vm: unreachable — is it up? start it with: %s" % state.yo_cmd("up")
            )
        if stage is BootStage.INITRD:
            ui.out(initrd_disk_line(unlock.initrd_state()))

    for line in disk_lines(vm_reachable):
        report.emit(line)

    if vm_reachable:
        try:
            status_guest_services(report)
        except sshx.Unreachable as lost:
            vm_reachable = False
            report.warn("vm: lost while probing — %s" % lost.message)

    gaps = state.driver().config_gaps()
    if gaps:
        report.warn(
            "%s: %s" % (state.driver().name, state.driver().config_gap_message(gaps))
        )
    else:
        ui.out("%s: reverse forwards ok" % state.driver().name)
    for gap in state.driver().host_gaps():
        report.emit(host_gap_line(gap))

    for line in aws_lines(vm_reachable):
        report.emit(line)

    if report.warnings:
        raise YoError(None, code=1)
