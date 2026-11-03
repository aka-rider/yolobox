"""Bringing the VM up, bootstrap, gc and disk growth."""

from __future__ import annotations

import argparse
import enum
import os
from typing import Optional, Tuple

from . import agent, forwards, guest, proc, release, sshconfig, sshx, state, ui, unlock
from .config import FILE_MODE_PRIVATE, GIB, LUKS_DEVICE
from .driver import NO_SIZE, RUNNING, Size
from .sshx import AGENT, OPERATOR
from .ui import Report, YoError
from .unlock import BootStage


LUKS_MAPPER = "root"

# The fullest the root may be for the first boot to encrypt it in place.
IN_PLACE_MAX_USED_PERCENT = 90
# What the migration needs beyond the shrunken filesystem: headroom_percent and
# two header_room_mib of 32 in nix/luks/yolobox-luks-migrate.sh
# (tests/test_consistency.py keeps them equal).
IN_PLACE_HEADROOM_PERCENT = 5
IN_PLACE_HEADER_ROOM_MIB = 64

# What of the disk /dev/vda2 does not get: the 249 MiB ESP (nix/base.md), the
# GPT tables and alignment, rounded up.
PARTITION_SLACK_BYTES = GIB

DISK_FSTYPE_PLAIN = "ext4"
DISK_FSTYPE_LUKS = "crypto_LUKS"


def vm_up(
    run_services: bool = True,
    passphrase: Optional[bytes] = None,
    size: Size = NO_SIZE,
) -> Optional[bytes]:
    gaps = state.driver().config_gaps()
    if state.driver().fatal_gaps(gaps):
        raise YoError(state.driver().config_gap_message(gaps))
    if gaps:
        ui.err("yo: %s" % state.driver().config_gap_message(gaps))
    status = state.driver().vm_status()
    if status is None:
        refusal = state.driver().creation_refusal(size)
        if refusal is not None:
            raise YoError(refusal)
    elif size.asked:
        # The one place for both drivers, and before the passphrase is asked for.
        raise YoError(state.driver().existing_size_refusal(size))
    # The one check for every start and unlock: up, both of bootstrap's and
    # disk-grow's. A cached passphrase needs no 1Password, but the logins that
    # follow do, and a 1Password that quit or locked since would otherwise
    # surface as a refused key (docs/linux-host.md#the-operators-key).
    state.driver().require_login_key()
    if status != RUNNING:
        passphrase = passphrase or agent.luks_passphrase()
        state.driver().start(passphrase, size)
    else:
        stage = unlock.boot_stage()
        if stage is BootStage.UNKNOWN:
            raise YoError(unknown_stage_message())
        if stage is BootStage.NONE:
            raise YoError(
                "%s is Running but neither its initrd nor its sshd answers on 127.0.0.1:%d "
                "— %s, %s"
                % (
                    state.box.name,
                    state.driver().ssh_port(),
                    state.driver().list_command(),
                    state.yo_cmd("status"),
                )
            )
        if stage is BootStage.INITRD:
            passphrase = passphrase or agent.luks_passphrase()
            unlock.unlock_disk(passphrase)
            unlock.wait_for_operator()
    sshconfig.ensure_agent_ssh_config()
    guest.ensure_guest_known_hosts()
    if run_services:
        forwards.vm_services()
    return passphrase


class DiskState(enum.Enum):
    UNREADABLE = "unreadable"
    PLAIN_EXT4 = "plain ext4"
    ENCRYPTED = "encrypted"
    OTHER = "other"


def classify_disk(fstype: str) -> DiskState:
    if not fstype:
        return DiskState.UNREADABLE
    if fstype == DISK_FSTYPE_PLAIN:
        return DiskState.PLAIN_EXT4
    if fstype == DISK_FSTYPE_LUKS:
        return DiskState.ENCRYPTED
    return DiskState.OTHER


def luks_device_fstype() -> str:
    # -d: the device alone. Without it lsblk lists the mapped child too, and on
    # the live box it printed the child's ext4 before the device's crypto_LUKS.
    return sshx.guest_out(["lsblk", "-dno", "FSTYPE", LUKS_DEVICE]).strip()


def guest_generations() -> Tuple[str, str]:
    # Not through yolobox-guest: bootstrap reads this on a fresh nixos-lima image,
    # which has no helper yet. The same pair as the helper's generations().
    lines = sshx.guest_out(
        [
            "sh",
            "-c",
            "readlink -f /nix/var/nix/profiles/system && readlink -f /run/current-system",
        ]
    ).splitlines()
    return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")


def booted_generations(report: Report) -> Tuple[str, str]:
    """The (profile, booted) generations, or an abort when the booted one is unreadable."""
    profile, booted = guest_generations()
    if not booted:
        raise report.abort(
            state.not_up_hint("cannot read the VM's generations", parenthesized=True)
        )
    return profile, booted


def in_place_encryption_warning() -> str:
    return (
        "yo: the next boot encrypts %s in place. The box is unusable until it finishes "
        "(this can take many minutes on a large disk), "
        "and the first minutes, an offline filesystem shrink, must not be interrupted: "
        'do not stop the VM or the host until %s reports "unlocked". '
        "%s first shrinks the work."
        % (LUKS_DEVICE, state.yo_cmd("up"), state.yo_cmd("gc", "--deep", "--yes"))
    )


def asked_size(args: argparse.Namespace) -> Size:
    """The size the --memory and --disk of `yo up` or `yo bootstrap` ask for."""
    return Size(args.memory, args.disk)


def cmd_up(args: argparse.Namespace) -> None:
    vm_up(size=asked_size(args))


def initrd_down_refusal(reading: unlock.InitrdReading) -> str:
    """Why `yo down` leaves a box in its initrd running, for every state but an idle wait.

    Whenever the box may be stuck for good, the refusal also prints the driver's
    own force-stop command and when it is safe to run: yo never runs it there.
    """
    name = state.box.name
    initrd = unlock.InitrdState
    force = state.driver().force_stop_command()
    journal = unlock.initrd_root_ssh_command("journalctl -u yolobox-luks-migrate -b")
    if reading.state is initrd.MIGRATING:
        # No force command: nothing here can make a kill safe.
        return (
            "%s is in its initrd, encrypting the disk in place, and killing the VM now "
            "can destroy the disk. Not stopping it. Wait, then finish the boot with %s "
            "and stop it with %s. To look inside first: %s"
            % (name, state.yo_cmd("up"), state.yo_cmd("down"), journal)
        )
    if reading.state is initrd.UNLOCKING:
        return (
            "%s is in its initrd with a yolobox-unlock helper running (its FIFO "
            "%s exists), so yo cannot prove it is idle. Not stopping it. No yo on this host is "
            "sending the passphrase right now, so it is usually the leftover of a "
            "%s that died, and it stays until the initrd restarts: this refusal "
            "would never end. Wait a minute (a live helper ends by itself) and run "
            "%s again. If it refuses again, power it off with: %s\n"
            "That is safe once no yo, on any machine, is unlocking this box: this "
            "probe saw no migration running, so nothing is encrypting."
            % (
                name,
                unlock.UNLOCK_FIFO_GLOB,
                state.yo_cmd("up"),
                state.yo_cmd("down"),
                force,
            )
        )
    if reading.state is initrd.MIGRATION_FAILED:
        return (
            "%s is in its initrd after its in-place disk encryption failed, and a "
            "polite stop would hang there. Not stopping it. Read why first: %s\n"
            "Powering off is safe from further damage: the migration script has "
            "ended, so nothing is writing, and the disk is as the script left it (see "
            "docs/disk-encryption.md#recognising-failures, a refusal for lack of "
            "space leaves the plaintext intact). Power it off with: %s"
            % (name, journal, force)
        )
    if reading.state is initrd.UNLOCKED_IDLE:
        return (
            "%s is in its initrd with the disk unlocked, and nothing is asking for a "
            "passphrase or running. After a %s that said the initrd could not mount "
            "the root (sysroot.mount failed), this is where the box waits in "
            "emergency mode, and %s cannot help it. Not stopping it. Check "
            "first: %s\n"
            "Nothing is writing, so powering off is safe once that journal shows the "
            "root mount failed and not a switch-root in progress. Power it off with: "
            "%s, then start it again with %s"
            % (
                name,
                state.yo_cmd("up"),
                state.yo_cmd("down"),
                unlock.initrd_root_ssh_command("journalctl -b"),
                force,
                state.yo_cmd("up"),
            )
        )
    return (
        "%s is in its initrd, and yo cannot prove it is idle: %s. Not stopping it. "
        "Look inside first: %s\n"
        "If it is encrypting the disk in place, killing the VM can destroy the "
        "disk, so wait for it. Powering it off is safe only when the journal shows "
        "no migration running: %s"
        % (name, reading.cause or reading.state.value, journal, force)
    )


def unknown_stage_message() -> str:
    return (
        "an sshd answers on 127.0.0.1:%d but refused both the operator's key and "
        "root's, so yo cannot tell whether %s waits in its initrd or runs; see %s"
        % (state.driver().ssh_port(), state.box.name, state.yo_cmd("status"))
    )


def cmd_down(args: argparse.Namespace) -> None:
    stage = unlock.boot_stage()
    if stage is BootStage.UNKNOWN:
        # Maybe an initrd, which ignores a shutdown request and may be encrypting
        # the disk, so neither stop is safe. A dead login key is the usual cause.
        state.driver().require_login_key()
        raise YoError("not stopping it: %s" % unknown_stage_message())
    if stage is not BootStage.INITRD:
        state.driver().stop()
        return
    # A box parked in its initrd has no ACPI handler, so a polite shutdown would
    # only wait out the stop timeout, and a forced one is safe only while nothing
    # is writing: the first boot's in-place encryption runs in this same initrd.
    # So it is powered off only when proved idle, and otherwise left alone: a
    # polite stop would hang, and a force would be a power cut mid-encryption.
    # The lock spans the probe and the power-off, so no yo can answer the
    # passphrase prompt between the proof and the kill.
    try:
        with state.unlock_lock(wait=False):
            reading = unlock.initrd_state()
            if reading.state is not unlock.InitrdState.WAITING_FOR_PASSPHRASE:
                raise YoError(initrd_down_refusal(reading))
            ui.out(
                "%s is waiting in its initrd for the passphrase, which cannot shut down; "
                "powering it off" % state.box.name
            )
            state.driver().force_stop()
    except state.UnlockBusy:
        raise YoError(
            "another yo is unlocking %s (%s, %s or %s is sending its passphrase). "
            "Not stopping it: its answer may start the in-place encryption. Wait "
            "for that command to finish, then run %s again"
            % (
                state.box.name,
                state.yo_cmd("up"),
                state.yo_cmd("bootstrap"),
                state.yo_cmd("disk-grow"),
                state.yo_cmd("down"),
            )
        )


def cmd_bootstrap(args: argparse.Namespace) -> None:
    report = Report()
    ref = args.version if args.version is not None else release.flake_ref(None)

    passphrase = vm_up(run_services=False, size=asked_size(args))
    report.ok("VM up")

    disk = classify_disk(luks_device_fstype())
    if disk is DiskState.UNREADABLE:
        raise YoError(
            "cannot read the type of %s — is the box up? %s"
            % (LUKS_DEVICE, state.yo_cmd("status"))
        )
    if disk is DiskState.PLAIN_EXT4:
        usage = guest.guest_root_usage()
        total, used = usage.total_kib, usage.used_kib
        if used * 100 > total * IN_PLACE_MAX_USED_PERCENT:
            raise YoError(
                "%s's root is %d%% full, over the %d%% the in-place encryption allows "
                "(it needs about %d%% headroom plus %d MiB) — reclaim first: %s"
                % (
                    state.box.name,
                    used * 100 // total,
                    IN_PLACE_MAX_USED_PERCENT,
                    IN_PLACE_HEADROOM_PERCENT,
                    IN_PLACE_HEADER_ROOM_MIB,
                    state.yo_cmd("gc", "--deep", "--yes"),
                )
            )
        ui.err(in_place_encryption_warning())

    sshx.ssh_run(
        OPERATOR,
        [
            "sudo",
            *release.nix_env(release.host_user(), ref),
            "nixos-rebuild",
            "boot",
            "--impure",
            "--flake",
            "%s#yolobox" % ref,
        ],
        mux=True,
        capture=False,
        check=True,
        inherit_stdin=True,
    )
    # Only an explicit --version is remembered: a default bootstrap means this
    # yo's release, so it forgets a fork ref instead of pinning it.
    # Any test that reaches this line must run under FakeHome.
    if args.version is not None:
        proc.private_dir(state.state_dir())
        proc.atomic_write(release.flake_ref_file(), ref + "\n", FILE_MODE_PRIVATE)
    else:
        try:
            os.remove(release.flake_ref_file())
        except FileNotFoundError:
            pass
    report.ok("built from %s" % ref)

    profile, booted = booted_generations(report)
    if profile == booted:
        report.ok("already running the built generation")
    else:
        # Derived before the VM stops, so a missing 1Password key leaves the box running.
        passphrase = passphrase or agent.luks_passphrase()
        try:
            state.driver().stop()
        except YoError as e:
            raise report.abort(
                state.driver().stop_failed(e.message),
                "The built generation is installed but not booted; the herdr machine",
                "and the 1Password/AWS forwards did not run.",
                *state.driver().stop_recovery(),
                "Then re-run %s: it boots the new generation, sees the two agree, "
                "and finishes." % state.yo_cmd("bootstrap"),
            )
        vm_up(run_services=False, passphrase=passphrase)
        profile, booted = booted_generations(report)
        if profile != booted:
            raise report.abort(
                "the VM rebooted but is still not running the built generation",
                "profile: %s" % profile,
                "booted:  %s" % booted,
                "Check df /boot in the VM — a half-failed bootloader install leaves the two",
                "generations disagreeing.",
            )
        report.ok("rebooted into the built generation")

    include_present = sshconfig.ensure_ssh_config_include(refuse=False)
    guest.ensure_guest_known_hosts()
    forwards.ensure_herdr_machine(include_present)

    forwards.vm_services()


def cmd_gc(args: argparse.Namespace) -> None:
    apply_flag = ["--apply"] if args.yes else []
    deep_flag = ["--deep"] if args.deep else []
    failed = False

    machine = guest.guest_helper(
        OPERATOR, "gc-machine", apply_flag, mux=True, stream=True
    )
    failed = failed or machine.returncode != 0

    sshx.require_agent_account()
    user = guest.guest_helper(
        AGENT, "gc-user", apply_flag + deep_flag, mux=True, stream=True
    )
    failed = failed or user.returncode != 0

    if failed:
        raise YoError(None, code=1)


def require_growable_guest() -> None:
    load_state = sshx.guest_out(
        ["systemctl", "show", "-p", "LoadState", "--value", "growpart"]
    ).splitlines()
    load_state = load_state[0] if load_state else ""
    if not load_state:
        raise YoError(
            state.not_up_hint(
                "cannot read the growpart unit from the VM", parenthesized=True
            )
        )
    if load_state != "loaded":
        raise YoError(
            "no growpart unit in the VM's current system, so growing the ceiling would leave /dev/vda2 untouched — switch it: %s"
            % release.rebuild_hint()
        )

    if classify_disk(luks_device_fstype()) is not DiskState.ENCRYPTED:
        raise YoError(
            "%s is not encrypted yet, so there is no mapping to grow — run: %s"
            % (LUKS_DEVICE, state.yo_cmd("bootstrap"))
        )


def vda2_size_for_report() -> str:
    """The partition's size as lsblk prints it, or why it could not be read: the grow
    is already done, so a failed read is reported in the line, not raised."""
    try:
        sizes = sshx.guest_out(["lsblk", "-no", "SIZE", "/dev/vda2"]).splitlines()
    except YoError as error:
        return "unreadable: %s" % error.message
    return sizes[0] if sizes else "unreadable: lsblk printed nothing"


def cmd_disk_grow(args: argparse.Namespace) -> None:
    wanted = args.size

    current_gib = state.driver().disk_ceiling_gib()
    if wanted < current_gib:
        raise YoError(
            "%s's ceiling is already %dGiB and %s can only grow it — ask for at least %d"
            % (state.box.name, current_gib, state.driver().name, current_gib)
        )
    # The ceiling edit is the one step that cannot be repeated, so asking for
    # the ceiling the instance already has finishes a grow that failed after it.
    finishing = wanted == current_gib

    if finishing:
        ui.out(
            "%s: %s already has a %dGiB ceiling; finishing a previous grow"
            % (state.driver().name, state.box.name, current_gib)
        )
        # Derived first: the box may be stopped or waiting in its initrd.
        passphrase = agent.luks_passphrase()
        vm_up(passphrase=passphrase)
        require_growable_guest()
    else:
        require_growable_guest()
        # Derived before the VM stops, so a missing 1Password key leaves the box running.
        passphrase = agent.luks_passphrase()

    try:
        # Before the ceiling is edited, the one step that cannot be repeated.
        before = guest.guest_root_usage()
    except YoError as error:
        raise YoError(state.not_up_hint(error.message))

    if not finishing:
        state.driver().grow_disk(current_gib, wanted)
        vm_up(passphrase=passphrase)
    # systemd-growfs cannot resize a keyring-keyed LUKS2 mapping, so the
    # mapping and then the filesystem are grown here, with the passphrase.
    sshx.ssh_run(
        OPERATOR,
        ["sudo", "cryptsetup", "resize", "--key-file", "-", LUKS_MAPPER],
        stdin=passphrase,
        check=True,
    )
    sshx.ssh_run(
        OPERATOR, ["sudo", "resize2fs", "/dev/mapper/%s" % LUKS_MAPPER], check=True
    )

    try:
        after = guest.guest_root_usage()
    except YoError as error:
        raise YoError(
            "the ceiling is now %dGiB but the grow is unverified: %s — check with: %s"
            % (wanted, error.message, state.yo_cmd("ssh", "df", "-k", "/"))
        )
    ui.out("before: %s" % before.human_line)
    ui.out("after:  %s" % after.human_line)
    ui.out("after:  vda2 is %s" % vda2_size_for_report())
    if after.total_kib > before.total_kib:
        return
    if finishing:
        # An earlier run may have grown the root already and failed afterwards.
        # But a growpart that failed at the previous boot leaves the partition
        # short, and then there is nothing left only for df.
        require_partition_grown(wanted)
        ui.out(
            "the root already fills the %dGiB ceiling; nothing left to grow" % wanted
        )
        return
    raise YoError(
        "the ceiling is now %dGiB but the guest root did not grow — diagnose with: %s"
        % (wanted, state.yo_cmd("ssh", "systemctl", "status", "growpart"))
    )


def require_partition_grown(ceiling_gib: int) -> None:
    """Raise unless /dev/vda2 fills the disk the ceiling describes, ESP aside."""
    sizes = sshx.guest_out(["lsblk", "-bdno", "SIZE", LUKS_DEVICE]).splitlines()
    size = sizes[0].strip() if sizes else ""
    if not proc.is_ascii_number(size):
        raise YoError(
            state.not_up_hint(
                "cannot read the size of %s (lsblk answered %r)" % (LUKS_DEVICE, size)
            )
        )
    if int(size) < ceiling_gib * GIB - PARTITION_SLACK_BYTES:
        raise YoError(
            "%s is %d bytes, short of the %dGiB ceiling: growpart did not grow it at "
            "the last boot — diagnose with: %s"
            % (
                LUKS_DEVICE,
                int(size),
                ceiling_gib,
                state.yo_cmd("ssh", "systemctl", "status", "growpart"),
            )
        )
