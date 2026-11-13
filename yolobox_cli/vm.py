"""Bringing the VM up, bootstrap, gc and disk growth."""

from __future__ import annotations

import argparse
import shlex
from typing import List, Optional, Tuple

from . import (
    agent,
    aws,
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


def preflight(size: Size, restart: bool) -> Tuple[Optional[VmStatus], Optional[NewBox]]:
    """The size flags and the host, judged before the box or the host is
    touched: no creation, resize, start or 1Password prompt is spent on a box
    that was going to be refused. Returns the box's status and the size a
    creation gets: exactly one of the two is None. `restart` says the box ran
    at this size a moment ago (bootstrap's reboot, disk-grow's restart), so its
    start adds no load for the host to refuse."""
    driver = state.driver()
    status = driver.vm_status()
    if status is None:
        refuse(driver.creation_refusal(size))
        refuse_start()
        # The default CPU count is decided here, so a host that cannot give one
        # is refused before the passphrase and both drivers create the same box.
        new_box = size.for_creation()
        refuse(driver.memory_refusal(size))
        return status, new_box
    if size.disk_gib is not None:
        refuse_disk_flag(size.disk_gib, driver.disk_gib())
    if size.asked and status.state is not VmState.OFF:
        raise YoError(started_box_refusal(status, size))
    if size.resizes:
        refuse(driver.resize_refusal(size))
    if status.state is not VmState.RUNNING:
        refuse_start()
    if status.state is VmState.OFF and not restart:
        refuse(driver.memory_refusal(size))
    return status, None


def refuse(refusal: Optional[str]) -> None:
    if refusal is not None:
        raise YoError(refusal)


def refuse_start() -> None:
    refuse(state.driver().start_refusal())


def refuse_disk_flag(wanted: int, current: int) -> None:
    """--disk on a box that exists: a size it has is fine, a smaller one is
    refused, and a larger one is left to disk-grow, which grows the encrypted
    root inside the booted box. A box that is off cannot show whether it has
    one, and a refusal after the resize and the start would leave a half-done
    command."""
    refuse_shrink(wanted, current)
    if wanted > current:
        raise YoError(
            "--disk %d would grow %s's disk, which is %dGiB, and only %s does that: "
            "run it, and drop --disk from this command"
            % (wanted, state.box.name, current, state.yo_cmd("disk-grow", str(wanted)))
        )


def started_box_refusal(status: VmStatus, size: Size) -> str:
    """The refusal of a size flag on a box that is not off: a definition applies
    only to a box that starts, and `yo up` restarts nothing. One place builds the
    remedy, which names the commands that lead out of the state the box is in."""
    resume = state.driver().resume_hint(status.word)
    stop = "stop it" if resume is None else "%s, then stop it" % resume
    return (
        "%s is %s, so %s cannot apply to it; %s and start it at that size: %s && %s"
        % (
            state.box.name,
            status.word,
            " ".join(size.flags()),
            stop,
            state.yo_cmd("down"),
            state.yo_cmd("up", *size.flags()),
        )
    )


def vm_up(
    held: state.HeldBox,
    run_services: bool = True,
    passphrase: Optional[agent.PassphraseSource] = None,
    size: Size = NO_SIZE,
    restart: bool = False,
) -> agent.PassphraseSource:
    """Bring the box up, unlocked, under the caller's box lock; the passphrase
    source it used, which derives only when an initrd asks (a plaintext seed
    boots without 1Password). `restart`: the box ran at this size a moment ago
    in this command, so the host's free-memory check lets its start pass."""
    passphrase = passphrase or agent.PassphraseSource()
    power_up(held, passphrase, size, restart)
    sshconfig.write_ssh_config()
    guest.ensure_guest_known_hosts()
    if run_services:
        # The seed image is up and waits for its bootstrap, so that is said
        # and nothing is served; a bootstrapped box must serve the agent.
        if sshx.bootstrapped(mux=True):
            problem = sshx.broken_agent_login()
            if problem is not None:
                raise YoError(problem)
            forwards.vm_services()
        else:
            ui.err("yo: %s" % sshx.not_bootstrapped_message())
    return passphrase


def power_up(
    held: state.HeldBox,
    passphrase: agent.PassphraseSource,
    size: Size,
    restart: bool,
) -> None:
    """Create, resize, start and unlock the box as it needs, judged from what it
    is now, under the box lock; every check that can refuse runs before the
    first step that changes the box."""
    status, new_box = preflight(size, restart)
    if new_box is not None:
        state.driver().create(held, new_box)
        state.driver().start(held, passphrase)
    elif status.state is not VmState.RUNNING:
        # A box that exists may be encrypted, and its initrd asks for the
        # passphrase only after the start: a 1Password that cannot give the
        # key is refused while the box is still off.
        passphrase.require_key()
        if size.resizes:
            state.driver().resize(held, size)
        state.driver().start(held, passphrase)
    else:
        stage = unlock.boot_stage(held)
        if stage.kind is BootStage.UNKNOWN:
            raise YoError(unlock.unknown_stage_message(stage))
        if stage.kind is BootStage.NONE:
            raise YoError(
                "%s is Running but yo cannot reach it on 127.0.0.1:%d%s — %s"
                % (
                    state.box.name,
                    state.driver().ssh_facts().port,
                    unlock.stage_said(stage),
                    state.hint(state.driver().hints.where_to_look),
                )
            )
        if stage.kind is BootStage.INITRD:
            unlock.unlock_disk(held, passphrase)
            unlock.wait_for_operator()


def luks_device_fstype() -> str:
    """DISK_FSTYPE_LUKS or DISK_FSTYPE_PLAIN, as lsblk names the root device;
    any other answer is an error that shows it."""
    # -d: the device alone. Without it lsblk lists the mapped child too, and on
    # the live box it printed the child's ext4 before the device's crypto_LUKS.
    fstype = sshx.guest_out(["lsblk", "-dno", "FSTYPE", LUKS_DEVICE]).strip()
    if fstype not in (DISK_FSTYPE_LUKS, DISK_FSTYPE_PLAIN):
        raise YoError(
            state.not_up_hint(
                "lsblk names the type of %s '%s', neither %s nor %s"
                % (LUKS_DEVICE, ui.shown(fstype), DISK_FSTYPE_LUKS, DISK_FSTYPE_PLAIN)
            )
        )
    return fstype


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
    with state.box_lock(wait=True) as held:
        vm_up(held, size=asked_size(args))


def initrd_down_refusal(initrd: unlock.InitrdEndpoint) -> str:
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
            unlock.initrd_root_ssh_command("journalctl -b", initrd),
            state.hint(state.driver().hints.force_stop_command),
        )
    )


def box_busy_refusal(consequence: str, rerun: str) -> YoError:
    """The refusal of a command that needs the box lock while a yo holds it."""
    return YoError(
        "another yo is acting on %s (%s holds its lock). %s Wait for that command "
        "to finish, then run %s again"
        % (state.box.name, state.box_lock_holders(), consequence, rerun)
    )


def cmd_down(args: argparse.Namespace) -> None:
    try:
        with state.box_lock(wait=False) as held:
            stop_box(held)
    except common.LockBusy:
        raise box_busy_refusal(
            "Not stopping it: an up's answer may start the in-place encryption.",
            state.yo_cmd("down"),
        )


def stop_box(held: state.HeldBox) -> None:
    """`yo down` under the box lock, judged from the stage read under it."""
    stage = unlock.boot_stage(held)
    if stage.kind is BootStage.UNKNOWN:
        # Maybe an initrd, which ignores a shutdown request and may be encrypting
        # the disk, so neither stop is safe. On libvirt a dead or locked
        # 1Password is the usual cause.
        raise YoError("not stopping it: %s" % unlock.unknown_stage_message(stage))
    if stage.kind is not BootStage.INITRD:
        state.driver().stop(held)
        return
    # A box parked in its initrd has no ACPI handler, so a polite shutdown would
    # only wait out the stop timeout, and a forced one is safe only while nothing
    # is writing: the first boot's in-place encryption runs in this same initrd.
    # So it is powered off only when proved idle, and otherwise left alone: a
    # polite stop would hang, and a force would be a power cut mid-encryption.
    # The caller's lock spans the probe and the power-off, so no yo can answer
    # the passphrase prompt between the proof and the kill.
    gate = unlock.require_initrd(held)
    if not unlock.initrd_ask_pending(gate):
        raise YoError(initrd_down_refusal(gate))
    ui.out(
        "%s is waiting in its initrd for the passphrase, which cannot shut down; "
        "powering it off" % state.box.name
    )
    state.driver().force_stop(held)


def destruction_steps() -> List[Removal]:
    """Every piece of this machine that exists now, in the order to remove it.
    The broker goes before the driver's pieces: it lives in the instance
    directory, and one that ends with its box's keepalive process unlinks
    its sockets while that directory is being deleted."""
    steps = (
        sshx.agent_master_removals() + aws.broker_removals() + state.driver().removals()
    )
    herdr = forwards.herdr_binary()
    if herdr is not None:
        steps += forwards.herdr_machine_removals(herdr)
    return steps


def cmd_destroy(args: argparse.Namespace) -> None:
    if not args.yes:
        list_destruction()
        return
    # The lock comes before the list: a yo up can create or start the box, and a
    # list made earlier would leave pieces behind or a box running at its
    # undefine. The listing alone changes nothing, so it takes no lock.
    try:
        with state.box_lock(wait=False) as held:
            destroy(held)
    except common.LockBusy:
        raise box_busy_refusal("Not destroying it.", state.yo_cmd("destroy", "--yes"))


def list_destruction() -> None:
    steps = destruction_steps()
    if not steps:
        ui.out("%s has nothing to destroy" % state.box.name)
        return
    ui.out("Destroying %s would:" % state.box.name)
    for step in steps:
        ui.out("  - %s" % step.what)
    ui.out(
        "Everything on %s is lost. Run %s to do it."
        % (state.box.name, state.yo_cmd("destroy", "--yes"))
    )


def destroy(held: state.HeldBox) -> None:
    steps = destruction_steps()
    if not steps:
        ui.out("%s has nothing to destroy" % state.box.name)
    for step in steps:
        ui.out(step.what)
        step.remove(held)


@agent.clearing_passphrases
def cmd_bootstrap(args: argparse.Namespace) -> None:
    # Under the box lock from the first read to the last step: a `yo down`
    # must not stop the box mid-rebuild or between its stop and its start.
    with state.box_lock(wait=True) as held:
        bootstrap(held, args)


def bootstrap(held: state.HeldBox, args: argparse.Namespace) -> None:
    report = Report()
    ref = args.version if args.version is not None else release.flake_ref(None)

    passphrase = vm_up(held, run_services=False, size=asked_size(args))
    report.ok("VM up")

    if luks_device_fstype() == DISK_FSTYPE_PLAIN:
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
        # Derived before the VM stops, so a missing 1Password key leaves the box
        # running; the reboot's initrd asks for it to encrypt the plain root.
        passphrase()

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
            state.driver().stop(held)
        except StopRefused as e:
            raise stop_aborted(e.reason)
        except YoError as e:
            raise stop_aborted(e.message)
        vm_up(held, run_services=False, passphrase=passphrase, restart=True)
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
    forwards.ensure_herdr_machine(held, include_present)

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


def refuse_shrink(wanted: int, current: int) -> None:
    if wanted < current:
        raise YoError(
            "%s's disk is already %dGiB and %s can only grow it — ask for at least %d"
            % (state.box.name, current, state.driver().name, current)
        )


@agent.clearing_passphrases
def cmd_disk_grow(args: argparse.Namespace) -> None:
    # Under the box lock from the size read to the resize: a `yo down` must not
    # stop the box mid-resize, and no other yo may grow it meanwhile.
    with state.box_lock(wait=True) as held:
        grow_disk(held, args.size)


def grow_disk(held: state.HeldBox, wanted: int) -> None:
    current = state.driver().disk_gib()
    # Refused before anything is spent on the box.
    refuse_shrink(wanted, current)
    # The box up first: the reads below need its guest, and vm_up's preflight
    # judges the box before anything is spent on it.
    passphrase = vm_up(held)
    require_encrypted_root()
    before = guest.guest_root_usage()
    # Derived before the stop, so a missing 1Password key leaves the box running.
    key = passphrase()
    if wanted != current:
        # The partition grows at the next boot (boot.growPartition).
        state.driver().stop(held)
        state.driver().grow_disk(held, wanted)
        vm_up(held, passphrase=passphrase, restart=True)
    grow_mapping_and_filesystem(key)
    after = guest.guest_root_usage()
    ui.out("before: %s" % before.human_line)
    ui.out("after:  %s" % after.human_line)
