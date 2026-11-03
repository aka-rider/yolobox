"""Bringing the VM up, bootstrap, gc and disk growth."""

from __future__ import annotations

import argparse
import enum
import os
from typing import Optional, Tuple

from . import agent, forwards, guest, proc, release, sshconfig, sshx, state, ui, unlock
from .config import FILE_MODE_PRIVATE, GIB, LUKS_DEVICE
from .driver import NO_SIZE, Size
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
    if gaps and state.driver().gaps_are_fatal:
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
    if status != "Running":
        passphrase = passphrase or agent.luks_passphrase()
        state.driver().start(passphrase, size)
    else:
        stage = unlock.boot_stage()
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
    return sshx.guest_out(["lsblk", "-dno", "FSTYPE", LUKS_DEVICE], check=False).strip()


def guest_generations() -> Tuple[str, str]:
    # Not through yolobox-guest: bootstrap reads this on a fresh nixos-lima image,
    # which has no helper yet. The same pair as the helper's generations().
    lines = sshx.guest_out(
        [
            "sh",
            "-c",
            "readlink -f /nix/var/nix/profiles/system && readlink -f /run/current-system",
        ],
        check=False,
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
            *release.nix_env(release.host_user()),
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
        ["systemctl", "show", "-p", "LoadState", "--value", "growpart"], check=False
    ).splitlines()
    load_state = load_state[0] if load_state else ""
    if not load_state:
        raise YoError(
            state.not_up_hint(
                "cannot reach the VM to ask about growpart", parenthesized=True
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
    vda2_after = sshx.guest_out(
        ["lsblk", "-no", "SIZE", "/dev/vda2"], check=False
    ).splitlines()
    ui.out("before: %s" % before.human_line)
    ui.out("after:  %s" % after.human_line)
    ui.out("after:  vda2 is %s" % (vda2_after[0] if vda2_after else ""))
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
    sizes = sshx.guest_out(
        ["lsblk", "-bdno", "SIZE", LUKS_DEVICE], check=False
    ).splitlines()
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
