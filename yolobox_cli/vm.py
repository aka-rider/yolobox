"""Bringing the VM up, bootstrap, gc and disk growth."""

from __future__ import annotations

import argparse
import contextlib
import shlex
from typing import List, Optional, Tuple

from . import (
    agent,
    common,
    forwards,
    guest,
    proc,
    release,
    sshconfig,
    sshx,
    state,
    ui,
    unlock,
)
from .config import LUKS_DEVICE, LUKS_MAPPER
from .driver import NO_SIZE, NewBox, Removal, Size, StopRefused, VmState, VmStatus
from .sshx import AGENT, OPERATOR
from .ui import Report, YoError
from .unlock import BootStage


# The fullest the root may be for the first boot to encrypt it in place.
IN_PLACE_MAX_USED_PERCENT = 90

DISK_FSTYPE_PLAIN = "ext4"
DISK_FSTYPE_LUKS = "crypto_LUKS"


def preflight(size: Size) -> Tuple[Optional[VmStatus], Optional[NewBox]]:
    """The size flags, judged before the box is touched: no start or 1Password
    prompt is spent on a box that was going to be refused. Returns the box's
    status and the size a creation gets (None: the box exists)."""
    status = state.driver().vm_status()
    if status is None:
        refusal = state.driver().creation_refusal(size)
        if refusal is not None:
            raise YoError(refusal)
        # The default CPU count is decided here, so a host that cannot give one
        # is refused before the passphrase and both drivers create the same box.
        return status, size.for_creation()
    if size.asked:
        # A box is sized when it is created; only its disk grows afterwards.
        raise YoError(
            "%s exists, and %s applies only when a box is created — drop the "
            "flag%s, or recreate the box, which deletes everything on it: %s. A "
            "disk grows in place: %s"
            % (
                state.box.name,
                " ".join(size.flags()),
                "s" if len(size.flags()) > 2 else "",
                state.driver().recreate_command(size),
                state.yo_cmd("disk-grow", "<GiB>"),
            )
        )
    return status, None


def vm_up(
    run_services: bool = True,
    passphrase: Optional[agent.Passphrase] = None,
    size: Size = NO_SIZE,
) -> Optional[agent.Passphrase]:
    status, new_box = preflight(size)
    if status is None or status.state is not VmState.RUNNING:
        # Derived before anything changes, so a missing 1Password key leaves the
        # box as it was.
        passphrase = passphrase or agent.luks_passphrase()
        state.driver().start(passphrase, new_box)
    else:
        stage = unlock.boot_stage()
        if stage is BootStage.UNKNOWN:
            raise YoError(unlock.unknown_stage_message())
        if stage is BootStage.NONE:
            raise YoError(
                "%s is Running but neither its initrd nor its sshd answers on 127.0.0.1:%d "
                "— %s"
                % (
                    state.box.name,
                    state.driver().ssh_facts().port,
                    state.hint(state.driver().hints.where_to_look),
                )
            )
        if stage is BootStage.INITRD:
            passphrase = passphrase or agent.luks_passphrase()
            unlock.unlock_disk(passphrase)
            unlock.wait_for_operator()
    sshconfig.write_ssh_config()
    guest.ensure_guest_known_hosts()
    if run_services:
        # The seed image has no agent account: it is up, and waits for its
        # bootstrap, so the problem is said and nothing is served.
        problem = sshx.agent_login_problem()
        if problem is None:
            forwards.vm_services()
        else:
            ui.err("yo: %s" % problem)
    return passphrase


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
    vm_up(size=asked_size(args))


def initrd_down_refusal() -> str:
    """`yo down` on a box in its initrd with no passphrase ask pending: the seed's
    first boot may be encrypting the disk, and a power cut there destroys it."""
    return (
        "%s is in its initrd and nothing there is waiting for the passphrase, so it "
        "may be encrypting its disk in place; not stopping it. Finish the boot with "
        "%s, then %s. To look inside: %s\n"
        "If you know nothing is encrypting (the box was bootstrapped before this "
        "boot), power it off with: %s"
        % (
            state.box.name,
            state.yo_cmd("up"),
            state.yo_cmd("down"),
            unlock.initrd_root_ssh_command("journalctl -b"),
            state.hint(state.driver().hints.force_stop_command),
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
        raise YoError("not stopping it: %s" % unlock.unknown_stage_message())
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
            if not unlock.initrd_ask_pending():
                raise YoError(initrd_down_refusal())
            ui.out(
                "%s is waiting in its initrd for the passphrase, which cannot shut down; "
                "powering it off" % state.box.name
            )
            state.driver().force_stop()
    except common.LockBusy:
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
    except common.LockBusy:
        raise unlock_busy_refusal(
            "Not destroying it.", state.yo_cmd("destroy", "--yes")
        )


@agent.clearing_passphrases
def cmd_bootstrap(args: argparse.Namespace) -> None:
    report = Report()
    ref = args.version if args.version is not None else release.flake_ref(None)

    passphrase = vm_up(run_services=False, size=asked_size(args))
    report.ok("VM up")

    fstype = luks_device_fstype()
    if not fstype:
        raise YoError(state.not_up_hint("cannot read the type of %s" % LUKS_DEVICE))
    if fstype == DISK_FSTYPE_PLAIN:
        usage = guest.guest_root_usage()
        total, used = usage.total_kib, usage.used_kib
        if used * 100 > total * IN_PLACE_MAX_USED_PERCENT:
            raise YoError(
                "%s's root is %d%% full, over the %d%% the in-place encryption allows "
                "— reclaim first: %s"
                % (
                    state.box.name,
                    used * 100 // total,
                    IN_PLACE_MAX_USED_PERCENT,
                    state.yo_cmd("gc", "--deep", "--yes"),
                )
            )
        ui.err(in_place_encryption_warning())

    # Resolved in the box first, so a branch that did not move, or one nix
    # would have served from an old fetch, shows before the build; then that
    # very commit is built, whatever the branch does meanwhile.
    source = release.locked_rev(ref)
    report.ok("building %s at %s (%s)" % (ref, source.rev, source.nar_hash))
    pinned = release.pinned_ref(ref, source)
    rebuild_argv = release.nixos_rebuild_argv("boot", release.host_user(), pinned)
    # Streamed, so every line nix prints shows behind the guest prefix like a gc
    # tier's; the session has no tty either way, and the exit code is read as is.
    rebuild = sshx.ssh_run(OPERATOR, rebuild_argv, mux=True, filtered=True)
    if rebuild.returncode == release.REBUILD_BUSY_EXIT:
        raise YoError(release.rebuild_busy_message())
    proc.checked(rebuild, shlex.join(rebuild_argv))
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
                "the box did not stop: %s" % detail,
                "The built generation is installed but not booted; the herdr machine",
                "and the 1Password/AWS forwards did not run.",
                "It runs stage 2 (nixos-rebuild just ran in it), so a hard power-off",
                "cuts no disk encryption: %s"
                % state.hint(state.driver().hints.force_stop_command),
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

    include_present = sshconfig.ensure_ssh_config_include(refuse=False)
    guest.ensure_guest_known_hosts()
    forwards.ensure_herdr_machine(include_present)

    sshx.require_agent_account()
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


def require_encrypted_root() -> None:
    """Only a bootstrapped box grows: the seed image has no mapping to grow."""
    if luks_device_fstype() != DISK_FSTYPE_LUKS:
        raise YoError(
            "%s is not encrypted yet, so there is no mapping to grow — run: %s"
            % (LUKS_DEVICE, state.yo_cmd("bootstrap"))
        )


def grow_mapping_and_filesystem(passphrase: agent.Passphrase) -> None:
    """Grow the LUKS mapping and the filesystem to the partition; both are
    no-ops at full size, so a grow that failed halfway is run again as is."""
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


@agent.clearing_passphrases
def cmd_disk_grow(args: argparse.Namespace) -> None:
    wanted = args.size
    current = state.driver().disk_gib()
    if wanted < current:
        raise YoError(
            "%s's disk is already %dGiB and %s can only grow it — ask for at least %d"
            % (state.box.name, current, state.driver().name, current)
        )
    # The box up first: the reads below need its guest, and vm_up's preflight
    # judges the box before anything is spent on it.
    passphrase = vm_up()
    require_encrypted_root()
    before = guest.guest_root_usage()
    # Derived before the stop, so a missing 1Password key leaves the box running.
    passphrase = passphrase or agent.luks_passphrase()
    if wanted > current:
        # The ceiling is raised only while the VM is off; the partition grows
        # at the next boot (boot.growPartition).
        state.driver().stop()
        state.driver().grow_disk(wanted)
        vm_up(passphrase=passphrase)
    grow_mapping_and_filesystem(passphrase)
    after = guest.guest_root_usage()
    ui.out("before: %s" % before.human_line)
    ui.out("after:  %s" % after.human_line)
