"""Bringing the VM up, bootstrap, gc and disk growth."""

from __future__ import annotations

import argparse
import contextlib
import enum
import os
import re
import shlex
from typing import List, NamedTuple, Optional, Tuple

from . import agent, forwards, guest, proc, release, sshconfig, sshx, state, ui, unlock
from .config import FILE_MODE_PRIVATE, GIB, LUKS_DEVICE, MIB
from .driver import NO_SIZE, RUNNING, BoxSize, Removal, Size, StopRefused
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
# What a grown LUKS mapping may lack of the partition (its header: the
# migration reserves header_room_mib of 32, see IN_PLACE_HEADER_ROOM_MIB) and a
# grown filesystem of its mapping (under one block), rounded up.
MAPPING_SLACK_BYTES = 64 * MIB

DISK_FSTYPE_PLAIN = "ext4"
DISK_FSTYPE_LUKS = "crypto_LUKS"

# What unknown_stage_message tells yo up and yo down to do once the login works.
RERUN = "run the command again"


class Resize(NamedTuple):
    """A stopped box's size now, and the size `yo up` was asked to give it."""

    current: BoxSize
    wanted: BoxSize

    @property
    def sets_hardware(self) -> bool:
        return (self.wanted.memory_gib, self.wanted.cpus) != (
            self.current.memory_gib,
            self.current.cpus,
        )


def plan_resize(status: str, asked: Size) -> Optional[Resize]:
    """What the size asked of a box that exists changes, or None for no change.

    A flag that equals the current value, or was not given, changes nothing.
    Everything that cannot be applied is refused here, before the passphrase
    and before anything is changed: a smaller disk, and any change to a box
    that is not stopped (it is never silently ignored).
    """
    if not asked.asked:
        return None
    driver = state.driver()
    current = driver.current_size()
    wanted = asked.fill(current)
    if wanted == current:
        return None
    if wanted.disk_gib < current.disk_gib:
        raise YoError(
            "%s's disk is %d GiB, and --disk %d is smaller: neither lima nor libvirt "
            "can shrink a disk — ask for at least %d, or drop --disk"
            % (state.box.name, current.disk_gib, wanted.disk_gib, current.disk_gib)
        )
    if status != driver.off_status:
        changed = current.changes_to(wanted)
        # A running box can have its disk grown alone, without stopping it first.
        grow_alone = (
            ", or, to only grow the disk of a running box, %s"
            % state.yo_cmd("disk-grow", str(wanted.disk_gib))
            if status == RUNNING and changed.memory_gib is None and changed.cpus is None
            else ""
        )
        raise YoError(
            "%s is %s with %s; %s would change that, and a box is resized only "
            "while it is stopped — %s, then run this command again%s"
            % (
                state.box.name,
                status,
                current.describe(),
                " ".join(changed.flags()),
                state.yo_cmd("down"),
                grow_alone,
            )
        )
    refusal = driver.resize_refusal(current, wanted)
    if refusal is not None:
        raise YoError(refusal)
    return Resize(current, wanted)


class Preflight(NamedTuple):
    """What preflight() found: the box's status, the size a creation gets, and
    the change a resize makes (None for none)."""

    status: Optional[str]
    size: Size
    resize: Optional[Resize]


def preflight(size: Size) -> Preflight:
    """Every check before the box is touched, in one place for a start, an
    unlock and the stop of a disk grow: the instance's config gaps, the asked
    size against the box as it is, and the login key. Nothing after it is
    refused for a reason that was knowable before, so no stop, ceiling edit or
    1Password prompt is spent on a box that was going to be refused."""
    gaps = state.driver().config_gaps()
    if state.driver().fatal_gaps(gaps):
        raise YoError(state.driver().config_gap_message(gaps))
    if gaps:
        ui.err("yo: %s" % state.driver().config_gap_message(gaps))
    status = state.driver().vm_status()
    resize = None
    if status is None:
        refusal = state.driver().creation_refusal(size)
        if refusal is not None:
            raise YoError(refusal)
        # The default CPU count is decided here, so a host that cannot give one
        # is refused before the passphrase and both drivers create the same box.
        size = size.for_creation()
    else:
        # The one place for both drivers, and before the passphrase is asked for.
        resize = plan_resize(status, size)
    # A cached passphrase needs no 1Password, but the logins that follow do,
    # and a 1Password that quit or locked since would otherwise surface as a
    # refused key (docs/linux-host.md#the-operators-key).
    state.driver().require_login_key()
    return Preflight(status, size, resize)


def vm_up(
    run_services: bool = True,
    passphrase: Optional[bytes] = None,
    size: Size = NO_SIZE,
) -> Optional[bytes]:
    status, size, resize = preflight(size)
    if status != RUNNING:
        # Derived before anything changes, so a missing 1Password key leaves the
        # box as it was.
        passphrase = passphrase or agent.luks_passphrase()
        if resize is not None and resize.sets_hardware:
            state.driver().resize(resize.wanted.memory_gib, resize.wanted.cpus)
        # `size` creates a box that is missing; a box that exists is already sized,
        # and a bigger disk is grown after the start (grow_to_asked_disk).
        state.driver().start(passphrase, size if status is None else NO_SIZE)
    else:
        stage = unlock.boot_stage()
        if stage is BootStage.UNKNOWN:
            raise YoError(unknown_stage_message(None, RERUN))
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
    """The size the --memory, --disk and --cpus of `yo up` or `yo bootstrap` ask for."""
    return Size(args.memory, args.disk, args.cpus)


@agent.clearing_passphrases
def cmd_up(args: argparse.Namespace) -> None:
    size = asked_size(args)
    passphrase = vm_up(size=size)
    if size.disk_gib is not None:
        grow_to_asked_disk(size.disk_gib, passphrase)


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


def login_problem() -> Optional[str]:
    """Why the box's login key is not on offer (on libvirt, 1Password's agent);
    None when it is, and always on Lima, whose key is a file."""
    try:
        state.driver().require_login_key()
    except YoError as failure:
        return failure.message
    return None


def unknown_stage_message(login: Optional[str], rerun: str) -> str:
    """What BootStage.UNKNOWN means and the way out of it; `login` is the login
    problem login_problem() found, or None; `rerun` says what to run once the
    login is fixed.

    It never offers a forced stop: a refusal proves nothing about the boot. A
    listed key may still fail to sign (a dismissed 1Password prompt), root's
    probe can time out or reset, and another yo may already have delivered the
    passphrase, so the initrd may be encrypting the disk. Only initrd_state(),
    run once yo can log in again, can tell when a stop is safe; what a person
    can do when the login is already right is the driver's (unknown_next_step)."""
    cause = " Found: %s." % login if login else ""
    return (
        "an sshd answers on 127.0.0.1:%d without the stage-2 banner and refused both "
        "the operator's key and root's, so yo cannot tell whether %s waits in its "
        "initrd, perhaps encrypting its disk, or runs.%s Fix the login (%s), then "
        "%s: yo then looks inside the initrd and stops it only when that is safe. "
        "%s"
        % (
            state.driver().ssh_port(),
            state.box.name,
            cause,
            state.driver().login_fix(),
            rerun,
            state.driver().unknown_next_step(),
        )
    )


def unlock_busy_refusal(consequence: str, rerun: str) -> YoError:
    """The refusal of a command that needs the unlock lock while a yo holds it."""
    return YoError(
        "another yo is unlocking %s (%s, %s or %s is sending its passphrase). %s "
        "Wait for that command to finish, then run %s again"
        % (
            state.box.name,
            state.yo_cmd("up"),
            state.yo_cmd("bootstrap"),
            state.yo_cmd("disk-grow"),
            consequence,
            rerun,
        )
    )


def cmd_down(args: argparse.Namespace) -> None:
    stage = unlock.boot_stage()
    if stage is BootStage.UNKNOWN:
        # Maybe an initrd, which ignores a shutdown request and may be encrypting
        # the disk, so neither stop is safe. On libvirt a dead or locked
        # 1Password is the usual cause.
        raise YoError(
            "not stopping it: %s" % unknown_stage_message(login_problem(), RERUN)
        )
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
    except state.LockBusy:
        raise unlock_busy_refusal(
            "Not stopping it: its answer may start the in-place encryption.",
            state.yo_cmd("down"),
        )


def destruction_steps() -> List[Removal]:
    """Every piece of this machine that exists now, in the order to remove it."""
    steps = sshx.agent_master_removals() + state.driver().removals()
    herdr = forwards.herdr_binary()
    if herdr is not None:
        steps += forwards.herdr_machine_removals(herdr)
    if os.path.exists(state.flake_ref_file()):
        steps.append(state.file_removal(state.flake_ref_file()))
    return steps


def cmd_destroy(args: argparse.Namespace) -> None:
    # The lock comes before the list: a yo that is unlocking can start a box that
    # is off, and a list made earlier would leave it running at its undefine. The
    # listing changes nothing, so it takes no lock.
    lock = state.unlock_lock(wait=False) if args.yes else contextlib.nullcontext()
    try:
        with lock:
            steps = destruction_steps()
            if not steps:
                ui.out("%s has nothing to destroy" % state.box.name)
            elif args.yes:
                for step in steps:
                    ui.out(step.what)
                    step.remove()
            else:
                ui.out("Destroying %s would:" % state.box.name)
                for step in steps:
                    ui.out("  - %s" % step.what)
                ui.out(
                    "Everything on %s is lost. Run %s to do it."
                    % (state.box.name, state.yo_cmd("destroy", "--yes"))
                )
    except state.LockBusy:
        raise unlock_busy_refusal(
            "Not destroying it.", state.yo_cmd("destroy", "--yes")
        )


@agent.clearing_passphrases
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

    # Resolved in the box first, so a branch that did not move, or one nix
    # would have served from an old fetch, shows before the build; then that
    # very commit is built, whatever the branch does meanwhile.
    rev = release.locked_rev(ref)
    report.ok("building %s at %s" % (ref, rev))
    rebuild_argv = release.nixos_rebuild_argv("boot", release.host_user(), ref, rev)
    # Streamed, so every line nix prints shows behind the guest prefix like a gc
    # tier's; the session has no tty either way, and the exit code is read as is.
    rebuild = sshx.ssh_run(OPERATOR, rebuild_argv, mux=True, filtered=True)
    if rebuild.returncode == release.REBUILD_BUSY_EXIT:
        raise YoError(release.rebuild_busy_message())
    if rebuild.returncode != 0:
        raise YoError(
            proc.failed_message(shlex.join(rebuild_argv), rebuild.returncode, None)
        )
    # Only an explicit --version is remembered: a default bootstrap means this
    # yo's release, so it forgets a fork ref instead of pinning it.
    # Any test that reaches this line must run under FakeHome.
    if args.version is not None:
        proc.private_dir(state.state_dir())
        proc.atomic_write(state.flake_ref_file(), ref + "\n", FILE_MODE_PRIVATE)
    else:
        proc.delete_file(state.flake_ref_file())
    report.ok("built from %s" % ref)

    profile, booted = booted_generations(report)
    if profile == booted:
        report.ok("already running the built generation")
    else:
        # Derived before the VM stops, so a missing 1Password key leaves the box running.
        passphrase = passphrase or agent.luks_passphrase()

        def stop_aborted(detail: str) -> YoError:
            # The box just ran nixos-rebuild, so it is in stage 2: say what
            # happened, then this context's recovery, never `yo down`'s
            # "do not force" advice beside a force that is safe here.
            return report.abort(
                state.driver().stop_failed(detail),
                "The built generation is installed but not booted; the herdr machine",
                "and the 1Password/AWS forwards did not run.",
                *state.driver().stop_recovery(),
                "Then re-run %s: it boots the new generation, sees the two agree, "
                "and finishes." % state.yo_cmd("bootstrap"),
            )

        try:
            state.driver().stop()
        except StopRefused as e:
            raise stop_aborted(e.reason)
        except YoError as e:
            raise stop_aborted(e.message)
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

    if args.disk is not None:
        # Only the built generation can grow its disk: the stock image grows
        # the partition at its first boot only, and the mapping and filesystem
        # of an encrypted root only with the passphrase, which yo holds.
        grow_to_asked_disk(args.disk, passphrase)

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


def root_usage_before_grow() -> guest.RootUsage:
    """The root's usage before the grow, read while the ceiling can still be left alone."""
    try:
        return guest.guest_root_usage()
    except YoError as error:
        raise YoError(state.not_up_hint(error.message))


def grow_running_disk(
    current_gib: int, wanted_gib: int, passphrase: Optional[bytes] = None
) -> None:
    """Raise the running box's disk from `current_gib` to `wanted_gib`, and grow into it.

    The one path of growing a disk that is not yet at its size, for
    `yo disk-grow` and for the disk of `yo up` and `yo bootstrap`
    (grow_to_asked_disk). The caller has checked that the guest can grow
    (require_growable_guest), while the ceiling was still untouched.
    """
    # The same checks as a start, before the stop: a config gap or a dead
    # 1Password would otherwise be found by the restart, with the box stopped.
    preflight(NO_SIZE)
    # Derived before the VM stops, so a missing 1Password key leaves the box running.
    passphrase = passphrase or agent.luks_passphrase()
    # Before the ceiling is edited, the one step that cannot be repeated.
    before = root_usage_before_grow()
    state.driver().stop()
    try:
        state.driver().grow_disk(current_gib, wanted_gib)
    except YoError as error:
        raise YoError(
            "%s\n%s is stopped and its disk is still %dGiB — try again with: %s"
            % (
                error.message,
                state.box.name,
                current_gib,
                state.yo_cmd("up", "--disk", str(wanted_gib)),
            ),
            error.code,
        )
    # From here the disk limit is wanted_gib, and asking for it again changes
    # nothing, so any failure names the command that finishes the growing.
    try:
        vm_up(passphrase=passphrase)
        grow_mapping_and_filesystem(passphrase, wanted_gib, before, finishing=False)
    except YoError as error:
        raise YoError(
            "%s\n%s's disk limit is already %dGiB; finish growing it with: %s"
            % (
                error.message,
                state.box.name,
                wanted_gib,
                state.yo_cmd("disk-grow", str(wanted_gib)),
            ),
            error.code,
        )


def grow_to_asked_disk(wanted_gib: int, passphrase: Optional[bytes]) -> None:
    """After `yo up --disk` or `yo bootstrap --disk` has the box up: grow its disk to `wanted_gib`.

    A box vm_up just created already has it. A box whose ceiling is already
    `wanted_gib` may have had a grow interrupted after the ceiling moved, so
    its mapping and filesystem are checked and finished as `yo disk-grow` does,
    and the passphrase is asked for only when one of them is short. A smaller
    disk was refused before this, by preflight() or cmd_disk_grow.
    """
    current_gib = state.driver().current_size().disk_gib
    if wanted_gib == current_gib:
        if root_has_room_to_grow(wanted_gib):
            finish_previous_grow(wanted_gib, passphrase or agent.luks_passphrase())
        else:
            ui.out(
                "the root already fills the %dGiB ceiling; nothing left to grow"
                % wanted_gib
            )
        return
    try:
        require_growable_guest()
    except YoError as error:
        # The rest of what was asked is done, so a rerun of this command is
        # refused as a change to a running box: name what grows the disk.
        raise YoError(
            "%s\n%s's disk is still %dGiB; once the guest can grow, grow it with: %s"
            % (
                error.message,
                state.box.name,
                current_gib,
                state.yo_cmd("disk-grow", str(wanted_gib)),
            ),
            error.code,
        )
    grow_running_disk(current_gib, wanted_gib, passphrase)


def grow_mapping_and_filesystem(
    passphrase: bytes, ceiling_gib: int, before: guest.RootUsage, finishing: bool
) -> None:
    """Grow the LUKS mapping and the filesystem into the raised ceiling, and verify."""
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
            % (ceiling_gib, error.message, state.yo_cmd("ssh", "df", "-k", "/"))
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
        require_partition_grown(ceiling_gib)
        ui.out(
            "the root already fills the %dGiB ceiling; nothing left to grow"
            % ceiling_gib
        )
        return
    raise YoError(
        "the ceiling is now %dGiB but the guest root did not grow — diagnose with: %s"
        % (ceiling_gib, state.yo_cmd("ssh", "systemctl", "status", "growpart"))
    )


def finish_previous_grow(ceiling_gib: int, passphrase: bytes) -> None:
    """On the running box, grow the mapping and filesystem into a ceiling already raised."""
    require_growable_guest()
    grow_mapping_and_filesystem(
        passphrase, ceiling_gib, root_usage_before_grow(), finishing=True
    )


@agent.clearing_passphrases
def cmd_disk_grow(args: argparse.Namespace) -> None:
    wanted = args.size

    current_gib = state.driver().current_size().disk_gib
    if wanted < current_gib:
        raise YoError(
            "%s's ceiling is already %dGiB and %s can only grow it — ask for at least %d"
            % (state.box.name, current_gib, state.driver().name, current_gib)
        )
    # The ceiling edit is the one step that cannot be repeated, so asking for
    # the ceiling the instance already has finishes a grow that failed after it.
    if wanted == current_gib:
        ui.out(
            "%s: %s already has a %dGiB ceiling; finishing a previous grow"
            % (state.driver().name, state.box.name, current_gib)
        )
    # The box up first, on both paths: the reads that decide what to grow need
    # its guest, and vm_up's preflight judges the box before anything is spent
    # on it. A box that is up and unlocked costs no passphrase here; the grow
    # of a bigger disk derives it before the stop, and heals the services on
    # the restart.
    passphrase = vm_up(run_services=wanted == current_gib)
    grow_to_asked_disk(wanted, passphrase)


def guest_device_bytes(device: str) -> int:
    """The size of a block device in the box, in bytes."""
    sizes = sshx.guest_out(["lsblk", "-bdno", "SIZE", device]).splitlines()
    size = sizes[0].strip() if sizes else ""
    if not proc.is_ascii_number(size):
        raise YoError(
            state.not_up_hint(
                "cannot read the size of %s (lsblk answered %r)" % (device, size)
            )
        )
    return int(size)


def guest_filesystem_bytes(device: str) -> int:
    """The size of the ext4 filesystem on `device`, from its superblock."""
    listing = sshx.guest_out(["sudo", "dumpe2fs", "-h", device])
    fields = {
        name: re.search(r"^%s:\s+(\d+)$" % name, listing, re.MULTILINE)
        for name in ("Block count", "Block size")
    }
    missing = [name for name, found in fields.items() if found is None]
    if missing:
        raise YoError(
            "cannot read the filesystem size of %s: dumpe2fs printed no %s"
            % (device, " or ".join(missing))
        )
    return int(fields["Block count"].group(1)) * int(fields["Block size"].group(1))


def root_has_room_to_grow(ceiling_gib: int) -> bool:
    """Whether the running box's mapping or filesystem is short of the device under it.

    The question behind `yo up --disk N` when the ceiling already is N: an
    earlier grow may have raised it and stopped before `cryptsetup resize` or
    `resize2fs`. A root that is not encrypted has no mapping, so a box not yet
    bootstrapped has nothing to finish. Reads only, so it asks for no passphrase.
    """
    fstype = luks_device_fstype()
    disk_state = classify_disk(fstype)
    if disk_state is DiskState.PLAIN_EXT4:
        return False
    if disk_state is not DiskState.ENCRYPTED:
        raise YoError(
            "cannot tell whether %s's root fills its disk: lsblk reports %r for %s"
            % (state.box.name, fstype, LUKS_DEVICE)
        )
    require_partition_grown(ceiling_gib)
    mapping = "/dev/mapper/%s" % LUKS_MAPPER
    mapping_bytes = guest_device_bytes(mapping)
    return (
        guest_device_bytes(LUKS_DEVICE) - mapping_bytes > MAPPING_SLACK_BYTES
        or mapping_bytes - guest_filesystem_bytes(mapping) > MAPPING_SLACK_BYTES
    )


def require_partition_grown(ceiling_gib: int) -> None:
    """Raise unless /dev/vda2 fills the disk the ceiling describes, ESP aside."""
    size = guest_device_bytes(LUKS_DEVICE)
    if size < ceiling_gib * GIB - PARTITION_SLACK_BYTES:
        raise YoError(
            "%s is %d bytes, short of the %dGiB ceiling: growpart did not grow it at "
            "the last boot — diagnose with: %s"
            % (
                LUKS_DEVICE,
                size,
                ceiling_gib,
                state.yo_cmd("ssh", "systemctl", "status", "growpart"),
            )
        )
