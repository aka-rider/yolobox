"""Unlocking the encrypted root through the initrd's sshd and waiting for the operator's sshd."""

from __future__ import annotations

import enum
import io
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import proc, release, sshconfig, state, ui
from .config import (
    FACTS,
    LOOPBACK,
    LUKS_DEVICE,
    LUKS_KEY_ITEM,
    SSH_UNREACHABLE,
    STAGE2_SSH_BANNER_MARK,
)
from .driver import NO_SIZE, SshFacts, VmState, VmStatus
from .ui import YoError


# yolobox-unlock's exit codes, each one verdict.
HELPER_EXIT_WRONG_KEY = FACTS["unlock_exit_wrong_key"]
HELPER_EXIT_ENCRYPT_FAILED = FACTS["unlock_exit_encrypt_failed"]
HELPER_EXIT_WATCH_LOST = FACTS["unlock_exit_watch_failed"]
HELPER_EXIT_NO_PASSPHRASE = FACTS["unlock_exit_no_passphrase"]
HELPER_EXIT_ROOT_MOUNT_FAILED = FACTS["unlock_exit_mount_failed"]
HELPER_EXIT_INTERNAL = FACTS["unlock_exit_unexpected"]
HELPER_EXIT_UNLOCK_FAILED = FACTS["unlock_exit_open_failed"]

# The script in the initrd that answers the disk's password asks.
INITRD_UNLOCK_HELPER = FACTS["initrd_unlock_helper"]
INITRD_WAIT_SECONDS = 600
OPERATOR_WAIT_SECONDS = 600
# Polls of the box: no event tells the host that a port on it came up.
INITRD_POLL_SECONDS = 2
OPERATOR_POLL_SECONDS = 3

# RFC 4253 caps the identification line at 255 bytes.
SSH_BANNER_MAX_BYTES = 255

NOT_LISTENING_MARKERS = (
    "Connection refused",
    "Connection reset",
    "kex_exchange_identification",
    "Connection closed",
    "closed by remote host",
    "Broken pipe",
    "Connection timed out",
    "Operation timed out",
)


class SshOutcome(enum.Enum):
    """What one ssh attempt at the initrd's or stage 2's sshd came to."""

    ANSWERED = "answered"  # the command ran and exited 0
    RAN = "ran"  # the command ran and exited non-zero
    REFUSED = "refused"  # sshd answered and refused the key
    NOT_LISTENING = "not-listening"  # no sshd behind the port (yet)
    ERROR = "error"  # ssh failed in a way yo has no name for


def classify_initrd_attempt(returncode: int, stderr: str) -> SshOutcome:
    if returncode != SSH_UNREACHABLE:
        return SshOutcome.RAN
    if "Permission denied" in stderr:
        return SshOutcome.REFUSED
    if any(marker in stderr for marker in NOT_LISTENING_MARKERS):
        return SshOutcome.NOT_LISTENING
    return SshOutcome.ERROR


def probe_sshd(endpoint: SshFacts, user: str) -> Tuple[SshOutcome, str]:
    """One `true` over the unpinned initrd channel as `user`, and ssh's stderr."""
    completed = proc.run(
        sshconfig.initrd_ssh_argv("true", endpoint, user=user), stdin=b"", guest=user
    )
    if completed.returncode == 0:
        return SshOutcome.ANSWERED, completed.stderr
    return (
        classify_initrd_attempt(completed.returncode, completed.stderr),
        completed.stderr,
    )


def sshd_answers(endpoint: SshFacts, user: str) -> SshOutcome:
    return probe_sshd(endpoint, user)[0]


def stage2_up(endpoint: SshFacts) -> bool:
    return sshd_answers(endpoint, release.host_user()) is SshOutcome.ANSWERED


class Endpoint(enum.Enum):
    """Which sshd the box's port offers, read from its banner before any login.

    The one classifier every login as root, and every passphrase, is gated on:
    both go only to an INITRD. Root is an invalid user on stage 2, which
    OpenSSH penalises per source address, and every host connection shares the
    gateway's address; a passphrase sent to stage 2 is a secret handed
    to the wrong sshd.
    """

    NONE = "none"  # no banner came: nothing listens, or nothing answered in time
    STAGE2 = "stage2"  # the banner carries the mark (VersionAddendum, nix/base.nix)
    # A banner without the mark: the initrd's, or the seed image's stage 2,
    # which only an operator login tells apart.
    INITRD = "initrd"


def endpoint_kind(endpoint: SshFacts) -> Endpoint:
    banner = ssh_banner(endpoint)
    if not banner:
        return Endpoint.NONE
    if STAGE2_SSH_BANNER_MARK in banner.split():
        return Endpoint.STAGE2
    return Endpoint.INITRD


class BootStage(enum.Enum):
    """Which sshd, if any, answers on the box's port."""

    STAGE2 = "stage2"  # the disk is unlocked and the operator can log in
    INITRD = "initrd"  # locked: waiting in the initrd for the passphrase
    NONE = "none"  # nothing answers, or a marked stage 2 refused the operator
    # An sshd answers without the stage-2 mark and refused both the operator
    # and root, so yo cannot tell an initrd (which may be encrypting the disk)
    # from a stage 2. On libvirt a dead or locked 1Password looks exactly like
    # this; on Lima it is an initrd without its keys.
    UNKNOWN = "unknown"


def ssh_banner(endpoint: SshFacts) -> str:
    """The identification line the sshd on the box's port sends before any
    login; "" when nothing answers. Reading it authenticates as nobody."""
    host, port = LOOPBACK, endpoint.port
    timeout = sshconfig.SSH_CONNECT_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    banner = b""
    # TCP may hand the line over in pieces, so read until its newline, within
    # the line's size cap and one deadline for the whole read. A sshd that
    # sends nothing in time, closes, or a port nothing listens on, leaves what
    # came; any other failure to connect is the host's, and an error.
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            while b"\n" not in banner and len(banner) < SSH_BANNER_MAX_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                conn.settimeout(remaining)
                chunk = conn.recv(SSH_BANNER_MAX_BYTES - len(banner))
                if not chunk:
                    break
                banner += chunk
    except (ConnectionRefusedError, ConnectionResetError, socket.timeout):
        pass
    except OSError as failure:
        raise YoError(
            "cannot read the ssh banner on %s:%d: %s"
            % (host, port, ui.os_error_reason(failure))
        )
    return banner.split(b"\n", 1)[0].decode("ascii", "replace")


def boot_stage() -> BootStage:
    # The one place that tells the two sshds apart, and it reads the banner
    # before it logs in anywhere: a port that sends none gets no login at all,
    # and root is offered only off a banner without the mark (Endpoint). Both
    # probes go through initrd_ssh_argv(), which pins nothing: a pinned ssh
    # against the initrd's per-boot host key would print REMOTE HOST
    # IDENTIFICATION HAS CHANGED.
    endpoint = state.driver().ssh_facts()
    kind = endpoint_kind(endpoint)
    if kind is Endpoint.NONE:
        return BootStage.NONE
    if stage2_up(endpoint):
        return BootStage.STAGE2
    if kind is Endpoint.STAGE2:
        return BootStage.NONE
    if sshd_answers(endpoint, "root") is SshOutcome.ANSWERED:
        return BootStage.INITRD
    return BootStage.UNKNOWN


ASK_PASSWORD_DIR = FACTS["ask_password_dir"]
# One command for the initrd's root shell, builtins only (no ls there): whether
# a password ask nobody has answered is waiting.
INITRD_ASK_PROBE = (
    'for f in %s/ask.*; do if [ -e "$f" ]; then echo ask=pending; exit 0; fi; done; '
    "echo ask=none" % ASK_PASSWORD_DIR
)


def initrd_ask_pending() -> bool:
    """Whether the initrd waits for a passphrase nobody has answered yet: the
    one moment nothing is being written to the disk, so the box may be powered
    off. An initrd that cannot be asked is an error, never a verdict."""
    completed = proc.run(
        sshconfig.initrd_ssh_argv(INITRD_ASK_PROBE, state.driver().ssh_facts()),
        stdin=b"",
        guest="root",
    )
    answer = completed.stdout.strip()
    if completed.returncode != 0 or answer not in ("ask=pending", "ask=none"):
        raise YoError(
            "cannot look inside %s's initrd: the probe exited %d%s"
            % (
                state.box.name,
                completed.returncode,
                proc.what_it_said(completed),
            )
        )
    return answer == "ask=pending"


def unknown_stage_message() -> str:
    """What BootStage.UNKNOWN means and the one way out. It never offers a
    forced stop: a refused login proves nothing about the boot, and another yo
    may already have delivered the passphrase, so the initrd may be encrypting
    the disk."""
    return (
        "an sshd answers on 127.0.0.1:%d without the stage-2 banner and refused both "
        "the operator's key and root's, so yo cannot tell whether %s waits in its "
        "initrd, perhaps encrypting its disk, or runs. Fix the login (%s), then run "
        "the command again"
        % (
            state.driver().ssh_facts().port,
            state.box.name,
            state.driver().hints.login_fix,
        )
    )


@dataclass(frozen=True)
class HelperRun:
    """One run of the initrd's yolobox-unlock over ssh."""

    returncode: int
    stderr: str
    streamed: bool  # the helper's stdout reached the terminal before the session ended


def run_initrd_helper(passphrase: bytes) -> HelperRun:
    """Send the passphrase to the initrd's helper and stream what it prints.

    The unlock lock is held for the whole run: `yo down` must never power the
    box off between the moment this answers the ask and the moment the probe
    can see that it did.
    """
    with state.unlock_lock(wait=True):
        stderr_lines: List[str] = []

        def collect(source: io.BufferedReader) -> None:
            for raw in iter(source.readline, b""):
                stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\n"))

        streamed = False
        # Read once: the listener that is checked is the one ssh then dials.
        endpoint = state.driver().ssh_facts()
        state.driver().verify_initrd_listener(endpoint.port)
        helper = proc.spawn(
            sshconfig.initrd_ssh_argv(INITRD_UNLOCK_HELPER, endpoint),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with helper as child:
            collector = threading.Thread(target=collect, args=(child.stderr,))
            collector.start()
            try:
                child.stdin.write(passphrase + b"\n")
                child.stdin.close()
            except BrokenPipeError:
                pass  # ssh already ended; its exit code and stderr, returned below, say why
            for raw in iter(child.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                streamed = True
                ui.out(ui.quoted_line(line))
            collector.join()
            returncode = child.wait()
        return HelperRun(returncode, "\n".join(stderr_lines), streamed)


class Verdict(enum.Enum):
    """What unlock_disk does after one helper run.

    UNLOCKED: the helper finished its job. FAILED: a failure retrying cannot
    fix. DROPPED: the session ran and then ended (switch-root, or an encryption
    that outlived the connection). REFUSED: sshd refused the key, and stage 2's
    sshd may be about to accept it. NOT_LISTENING: nothing to talk to yet.
    """

    UNLOCKED = "unlocked"
    FAILED = "failed"
    DROPPED = "dropped"
    REFUSED = "refused"
    NOT_LISTENING = "not-listening"


def judge_helper_run(attempt: HelperRun) -> Verdict:
    outcome = classify_initrd_attempt(attempt.returncode, attempt.stderr)
    if outcome is SshOutcome.RAN:
        return Verdict.UNLOCKED if attempt.returncode == 0 else Verdict.FAILED
    if attempt.streamed:
        return Verdict.DROPPED
    if outcome is SshOutcome.ERROR:
        return Verdict.FAILED
    if outcome is SshOutcome.REFUSED:
        return Verdict.REFUSED
    return Verdict.NOT_LISTENING


def initrd_root_ssh_command(remote_command: str) -> str:
    """The command yo itself would run, for a printed remedy to paste."""
    return shlex.join(
        sshconfig.initrd_ssh_argv(remote_command, state.driver().ssh_facts())
    )


def helper_failure_message(attempt: HelperRun) -> str:
    code, stderr = attempt.returncode, ui.quoted_block(attempt.stderr)
    if code == SSH_UNREACHABLE:
        return stderr or "ssh to 127.0.0.1:%d failed with exit %d and no message" % (
            state.driver().ssh_facts().port,
            code,
        )
    if code == HELPER_EXIT_ENCRYPT_FAILED:
        return (
            "the in-place encryption of %s failed in the initrd, and the box must be "
            "recreated: %s\n%s"
            % (LUKS_DEVICE, state.driver().recreate_command(NO_SIZE), stderr)
        )
    if code == HELPER_EXIT_ROOT_MOUNT_FAILED:
        return (
            "the disk unlocked but the initrd could not mount it as /sysroot "
            "(sysroot.mount failed); the box waits in its initrd emergency mode. "
            "Inspect it as root: %s\n%s"
            % (initrd_root_ssh_command("journalctl -b"), stderr)
        )
    if code == HELPER_EXIT_WRONG_KEY:
        return (
            "the %s key in 1Password does not open %s — a different key encrypted "
            "this disk, and there is no recovery slot" % (LUKS_KEY_ITEM, LUKS_DEVICE)
        )
    if code == HELPER_EXIT_UNLOCK_FAILED:
        return (
            "opening %s failed in the initrd, and not for the key; its journal:\n%s"
            % (LUKS_DEVICE, stderr)
        )
    if code == HELPER_EXIT_WATCH_LOST:
        return "the initrd helper lost its watch:\n%s" % stderr
    if code == HELPER_EXIT_NO_PASSPHRASE:
        return "no passphrase reached the initrd helper"
    if code == HELPER_EXIT_INTERNAL:
        return (
            "the initrd helper failed unexpectedly; the line below names the command "
            "that failed:\n%s" % stderr
        )
    return "the initrd helper failed with exit %d:\n%s" % (code, stderr)


def vm_lost_error(status: Optional[VmStatus], waiting_for: str) -> YoError:
    """The VM is not Running while yo waits for it (None: no longer there)."""
    return YoError(
        "%s is %s, not Running, while yo waited for %s — %s"
        % (
            state.box.name,
            status.word if status is not None else "not listed",
            waiting_for,
            state.hint(state.driver().hints.where_to_look),
        )
    )


def vm_is_running() -> Optional[VmStatus]:
    """The VM's status, when it is Running; else the error a wait ends with is the caller's."""
    status = state.driver().vm_status()
    return status if status is not None and status.state is VmState.RUNNING else None


def initrd_timeout_error(key_was_refused: bool) -> YoError:
    if key_was_refused:
        return YoError(
            "the sshd on 127.0.0.1:%d refused %s's key (root in an initrd, %s on a "
            "stage 2) — if %s is in its initrd, reading the key from cidata failed"
            % (
                state.driver().ssh_facts().port,
                state.driver().name,
                release.host_user(),
                state.box.name,
            )
        )
    return YoError(
        "%s never offered its initrd sshd on 127.0.0.1:%d within %d s of %s "
        "reporting it Running — %s"
        % (
            state.box.name,
            state.driver().ssh_facts().port,
            INITRD_WAIT_SECONDS,
            state.driver().name,
            state.hint(state.driver().hints.where_to_look),
        )
    )


def unlock_disk(passphrase: bytes) -> None:
    """Answer the initrd's passphrase ask and wait until the boot is past it.

    The VM is Running when this starts: the driver's start() has seen to it."""
    session_ran = False
    deadline = time.monotonic() + INITRD_WAIT_SECONDS
    refused_since: Optional[float] = None
    # Both outcomes are named: a seed image's disk is not encrypted yet, and its
    # boot ends here with no ask at all.
    ui.err(
        "yo: waiting for %s to boot: its initrd to ask for the disk passphrase, "
        "or its sshd to answer if the disk is not encrypted" % state.box.name
    )
    while True:
        # The banner decides what this round may do (Endpoint): the helper,
        # with the passphrase on its stdin as root, runs only against an sshd
        # without the stage-2 mark. A marked one is asked for the operator and
        # nothing else, so after a switch-root the new stage 2 never sees root.
        endpoint = state.driver().ssh_facts()
        kind = endpoint_kind(endpoint)
        # An unmarked sshd is asked for the operator first only once a session
        # ran: switch-root may have landed in the seed image's stage 2.
        operator_asked = kind is Endpoint.STAGE2 or (
            kind is Endpoint.INITRD and session_ran
        )
        if operator_asked:
            outcome = sshd_answers(endpoint, release.host_user())
            if outcome is SshOutcome.ANSWERED:
                return
            if outcome is SshOutcome.REFUSED:
                # A fresh instance's first plain boot writes the operator's key
                # moments after its sshd starts, so a refusal is retried.
                refused_since = refused_since or time.monotonic()
        if kind is Endpoint.INITRD:
            attempt = run_initrd_helper(passphrase)
            verdict = judge_helper_run(attempt)
            if verdict in (Verdict.UNLOCKED, Verdict.DROPPED):
                for line in attempt.stderr.splitlines():
                    ui.err(ui.quoted_line(line))
            if verdict is Verdict.UNLOCKED:
                return
            if verdict is Verdict.FAILED:
                raise YoError(helper_failure_message(attempt))
            if verdict is Verdict.DROPPED:
                # Switch-root, or an encryption that outlived the connection.
                # A fresh budget, since the old one may have expired while the
                # session streamed.
                session_ran = True
                deadline = time.monotonic() + INITRD_WAIT_SECONDS
            if verdict is Verdict.REFUSED:
                # Root refused by an unmarked sshd: an initrd without its keys,
                # or the seed image's stage 2, which may admit the operator by
                # now, or moments from now.
                if not operator_asked and stage2_up(endpoint):
                    return
                refused_since = refused_since or time.monotonic()

        status = vm_is_running()
        if status is None:
            raise vm_lost_error(
                state.driver().vm_status(), "its initrd to take the disk passphrase"
            )
        if time.monotonic() > deadline:
            raise initrd_timeout_error(refused_since is not None)
        time.sleep(INITRD_POLL_SECONDS)


def wait_for_operator() -> None:
    """Wait for stage 2's sshd to accept the operator, up to OPERATOR_WAIT_SECONDS.

    Every outcome but ANSWERED is retried until the deadline, since a box
    that is still starting sshd answers RAN, ERROR or a refused key for a
    while. Only the VM going away ends the wait early.
    """
    deadline = time.monotonic() + OPERATOR_WAIT_SECONDS
    endpoint = state.driver().ssh_facts()
    while True:
        # Bounded by ssh's ConnectTimeout and ServerAliveInterval.
        outcome, stderr = probe_sshd(endpoint, release.host_user())
        if outcome is SshOutcome.ANSWERED:
            return
        # First the VM: one that went away or paused (the host's OOM killer, a
        # crash) also fails a probe in shapes of its own, a keepalive timeout.
        if vm_is_running() is None:
            raise vm_lost_error(
                state.driver().vm_status(), "its sshd to accept the operator"
            )
        if time.monotonic() > deadline:
            raise YoError(
                "the disk is unlocked but %s is not reachable as the operator after "
                "%d s (the last ssh attempt: %s%s) — %s"
                % (
                    state.box.name,
                    OPERATOR_WAIT_SECONDS,
                    outcome.value,
                    proc.said_suffix(ui.quoted_block(stderr)),
                    state.yo_cmd("status"),
                )
            )
        time.sleep(OPERATOR_POLL_SECONDS)
