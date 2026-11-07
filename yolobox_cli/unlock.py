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
    LUKS_DEVICE,
    LUKS_KEY_ITEM,
    SSH_UNREACHABLE,
    STAGE2_SSH_BANNER_MARK,
)
from .driver import RUNNING
from .ui import YoError


# yolobox-unlock's exit codes; nix/luks/yolobox-unlock.sh documents the same
# table in its header and tests/test_consistency.py compares the two.
HELPER_EXIT_WRONG_KEY = 1
HELPER_EXIT_MIGRATION_FAILED = 2
HELPER_EXIT_WATCH_LOST = 3
HELPER_EXIT_NO_PASSPHRASE = 4
HELPER_EXIT_ROOT_MOUNT_FAILED = 5
HELPER_EXIT_INTERNAL = 6

# The script in the initrd that answers the disk's password asks (nix/luks).
INITRD_UNLOCK_HELPER = "yolobox-unlock"
# The password-ask id nix/luks/yolobox-luks-migrate.sh posts; seeing it means a
# migration ran in this boot.
MIGRATE_ASK_ID = "yolobox:luks-migrate"
INITRD_WAIT_SECONDS = 600
OPERATOR_WAIT_SECONDS = 600
# Polls of a machine outside yo, not orderings between yo's own events: nothing
# on the Mac can wait on a remote TCP port coming up.
INITRD_POLL_SECONDS = 2
OPERATOR_POLL_SECONDS = 3

# What `limactl list` reports before the guest is running: not listed yet (None),
# Installing, Starting, and Stopped, which an instance still says in the moments
# after `limactl start` is spawned and before its hostagent exists.
LIMA_PRE_BOOT_STATUSES: Tuple[Optional[str], ...] = (
    None,
    "Installing",
    "Starting",
    "Stopped",
)

# The yolobox-luks-migrate unit's description (nix/luks.nix), which the boot
# log shows as the unit starts and finishes encrypting the disk in place.
MIGRATE_UNIT_DESCRIPTION = "Encrypt or finish encrypting %s in place" % LUKS_DEVICE

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


# Whose session the initrd's text is, for ui.GuestText: root's there is the
# unlock login, and nothing else on the box answers as root.
INITRD_SESSION = "initrd root"


def session_of(user: str) -> str:
    """Whose words an unpinned probe as `user` brings back, for ui.GuestText."""
    return INITRD_SESSION if user == "root" else "operator"


def probe_sshd(endpoint: sshconfig.InitrdEndpoint, user: str) -> Tuple[SshOutcome, str]:
    """One `true` over the unpinned initrd channel as `user`, and ssh's stderr."""
    completed = proc.run(
        sshconfig.initrd_ssh_argv("true", endpoint, user=user),
        stdin=b"",
        guest=session_of(user),
    )
    if completed.returncode == 0:
        return SshOutcome.ANSWERED, completed.stderr
    return (
        classify_initrd_attempt(completed.returncode, completed.stderr),
        completed.stderr,
    )


def sshd_answers(endpoint: sshconfig.InitrdEndpoint, user: str) -> SshOutcome:
    return probe_sshd(endpoint, user)[0]


def stage2_up(endpoint: sshconfig.InitrdEndpoint) -> bool:
    return sshd_answers(endpoint, release.host_user()) is SshOutcome.ANSWERED


class Endpoint(enum.Enum):
    """Which sshd the box's port offers, read from its banner before any login.

    The one classifier every login as root, and every passphrase, is gated on:
    both go only to an INITRD. Root is an invalid user on stage 2, which
    OpenSSH penalises per source address, and every host connection shares the
    gateway's (nix/base.md); a passphrase sent to stage 2 is a secret handed
    to the wrong sshd.
    """

    NONE = "none"  # no banner came: nothing listens, or nothing answered in time
    STAGE2 = "stage2"  # the banner carries the mark (VersionAddendum, nix/base.nix)
    # A banner without the mark: the initrd's, or a stage 2 built before the
    # mark, which only an operator login tells apart.
    INITRD = "initrd"


def endpoint_kind(endpoint: sshconfig.InitrdEndpoint) -> Endpoint:
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
    # this; on Lima it is an initrd without its keys, or a stage 2 built before
    # the mark that refuses Lima's key.
    UNKNOWN = "unknown"


def ssh_banner(endpoint: sshconfig.InitrdEndpoint) -> str:
    """The identification line the sshd on the box's port sends before any
    login; "" when nothing answers. Reading it authenticates as nobody."""
    host, port, _ = endpoint
    timeout = sshconfig.SSH_CONNECT_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    banner = b""
    # TCP may hand the line over in pieces, so read until its newline, within
    # the line's size cap and one deadline for the whole read. A sshd that
    # sends nothing in time, or a port nothing listens on, leaves what came.
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
    except OSError:
        pass
    return banner.split(b"\n", 1)[0].decode("ascii", "replace")


def boot_stage() -> BootStage:
    # The one place that tells the two sshds apart, and it reads the banner
    # before it logs in anywhere: a port that sends none gets no login at all,
    # and root is offered only off a banner without the mark (Endpoint). Both
    # probes go through initrd_ssh_argv(), which pins nothing: a pinned ssh
    # against the initrd's per-boot host key would print REMOTE HOST
    # IDENTIFICATION HAS CHANGED.
    endpoint = sshconfig.initrd_endpoint()
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


class InitrdState(enum.Enum):
    """What the initrd is doing, as far as the facts below prove it.

    WAITING_FOR_PASSPHRASE is the only state in which nothing is being written
    to the disk and nobody is answering the ask, so it is the only one `yo down`
    may power off by itself. The others are named apart because what to do about
    them differs, and `yo down` says so.
    """

    WAITING_FOR_PASSPHRASE = "waiting for the passphrase"
    MIGRATING = "encrypting the disk in place"
    UNLOCKING = "unlocking the disk, or holding the leftover of an unlock"
    MIGRATION_FAILED = "stopped after a failed in-place encryption"
    UNLOCKED_IDLE = "unlocked, with nothing asking and nothing running"
    UNKNOWN = "in a state yo cannot prove idle"


@dataclass(frozen=True)
class InitrdReading:
    """One probe's verdict, and why when the probe could not be judged."""

    state: InitrdState
    cause: str  # empty unless the state is UNKNOWN


ASK_PASSWORD_DIR = "/run/systemd/ask-password"
UNLOCK_FIFO_GLOB = "/tmp/yolobox-unlock.*"
PROBE_MIGRATE_PREFIX = "migrate="
PROBE_FIFO_PREFIX = "unlock-fifo="
PROBE_ASK_BEGIN = "ask-begin"
PROBE_ASK_END = "ask-end"
PROBE_END = "probe=end"
ASK_ID_PREFIX = "Id="
# systemd-cryptsetup posts its ask as "cryptsetup:<device>".
CRYPTSETUP_ASK_ID_PREFIX = "cryptsetup:"
# The oneshot unit of nix/luks/yolobox-luks-migrate.sh is "activating" for the
# whole run of the script, from waiting for its passphrase through the last write.
MIGRATE_UNIT_RUNNING = "activating"
MIGRATE_UNIT_FINISHED = "active"
MIGRATE_UNIT_FAILED = "failed"


def initrd_probe_command(ask_dir: str, fifo_glob: str) -> str:
    """One command for the initrd's root shell: shell builtins, cat and systemctl.

    Sed, grep and ls are not on the initrd's PATH. Every section announces
    itself with a marker line, so the classifier never depends on whether a file
    ends in a newline: the migration unit's state; one `unlock-fifo=` line per
    FIFO that a running yolobox-unlock keeps in /tmp (nix/luks/yolobox-unlock.sh);
    each pending ask file between `ask-begin` and `ask-end`; and a last line
    that shows the command ran to its end.
    """
    return (
        "echo %(migrate)s$(systemctl is-active yolobox-luks-migrate.service); "
        'for f in %(fifos)s; do [ -e "$f" ] && echo "%(fifo)s$f"; done; '
        'for f in %(asks)s/ask.*; do [ -e "$f" ] || continue; '
        'echo %(begin)s; cat "$f"; echo; echo %(end)s; done; '
        "echo %(final)s"
    ) % {
        "migrate": PROBE_MIGRATE_PREFIX,
        "fifos": fifo_glob,
        "fifo": PROBE_FIFO_PREFIX,
        "asks": ask_dir,
        "begin": PROBE_ASK_BEGIN,
        "end": PROBE_ASK_END,
        "final": PROBE_END,
    }


INITRD_PROBE_COMMAND = initrd_probe_command(ASK_PASSWORD_DIR, UNLOCK_FIFO_GLOB)


def classify_initrd_probe(output: str) -> InitrdState:
    """Judge the output of INITRD_PROBE_COMMAND; anything unexpected is UNKNOWN."""
    migrate_states: List[str] = []
    ask_ids: List[str] = []
    unlock_running = False
    in_ask = False
    ended = False
    for line in output.splitlines():
        if ended:
            return InitrdState.UNKNOWN
        if in_ask:
            if line == PROBE_ASK_END:
                in_ask = False
            elif line.startswith(ASK_ID_PREFIX):
                ask_ids.append(line[len(ASK_ID_PREFIX) :])
        elif line == PROBE_ASK_BEGIN:
            in_ask = True
        elif line.startswith(PROBE_MIGRATE_PREFIX):
            migrate_states.append(line[len(PROBE_MIGRATE_PREFIX) :])
        elif line.startswith(PROBE_FIFO_PREFIX):
            unlock_running = True
        elif line == PROBE_END:
            ended = True
        else:
            return InitrdState.UNKNOWN
    if not ended or in_ask or len(migrate_states) != 1:
        return InitrdState.UNKNOWN
    migrate = migrate_states[0]
    migrate_ask_pending = MIGRATE_ASK_ID in ask_ids
    # The ask is the single moment the migration is provably before its first write.
    if migrate == MIGRATE_UNIT_RUNNING and not migrate_ask_pending:
        return InitrdState.MIGRATING
    if unlock_running:
        return InitrdState.UNLOCKING
    if migrate == MIGRATE_UNIT_FAILED:
        return InitrdState.MIGRATION_FAILED
    if migrate not in (MIGRATE_UNIT_RUNNING, MIGRATE_UNIT_FINISHED):
        return InitrdState.UNKNOWN
    if not ask_ids:
        # Only a finished unit gets here (a running one with no ask is MIGRATING).
        return InitrdState.UNLOCKED_IDLE
    if all(
        ask_id == MIGRATE_ASK_ID or ask_id.startswith(CRYPTSETUP_ASK_ID_PREFIX)
        for ask_id in ask_ids
    ):
        return InitrdState.WAITING_FOR_PASSPHRASE
    return InitrdState.UNKNOWN


def initrd_state() -> InitrdReading:
    completed = proc.run(
        sshconfig.initrd_ssh_argv(INITRD_PROBE_COMMAND, sshconfig.initrd_endpoint()),
        stdin=b"",
        guest=INITRD_SESSION,
    )
    if completed.returncode != 0:
        return InitrdReading(
            InitrdState.UNKNOWN,
            "the probe's ssh exited %d%s"
            % (completed.returncode, proc.what_it_said(completed)),
        )
    verdict = classify_initrd_probe(completed.stdout)
    if verdict is InitrdState.UNKNOWN:
        return InitrdReading(
            verdict,
            "the probe printed what no known initrd state matches:\n%s"
            % ui.quoted_block(completed.stdout),
        )
    return InitrdReading(verdict, "")


@dataclass(frozen=True)
class HelperRun:
    """One run of the initrd's yolobox-unlock over ssh."""

    returncode: int
    stderr: str
    migrated: bool
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

        migrated = False
        streamed = False
        # Read once: the listener that is checked is the one ssh then dials.
        endpoint = sshconfig.initrd_endpoint()
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
                migrated = migrated or "id=%s" % MIGRATE_ASK_ID in line
                ui.out(ui.quoted_line(line))
            collector.join()
            returncode = child.wait()
        return HelperRun(
            returncode,
            ui.GuestText("\n".join(stderr_lines), INITRD_SESSION),
            migrated,
            streamed,
        )


class Verdict(enum.Enum):
    """What unlock_disk does after one helper run.

    UNLOCKED: the helper finished its job. FAILED: a failure retrying cannot
    fix. DROPPED: the session ran and then ended (switch-root, or a migration
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
        sshconfig.initrd_ssh_argv(remote_command, sshconfig.initrd_endpoint())
    )


def helper_failure_message(attempt: HelperRun) -> str:
    code, stderr = attempt.returncode, ui.quoted_block(attempt.stderr)
    if code == SSH_UNREACHABLE:
        return stderr or "ssh to 127.0.0.1:%d failed with exit %d and no message" % (
            state.driver().ssh_port(),
            code,
        )
    if code == HELPER_EXIT_MIGRATION_FAILED:
        return (
            "the in-place encryption failed in the initrd; the box is still in its "
            "initrd, and the last journal line below says what the disk holds now "
            "(still plaintext, partly encrypted, or encrypted but not finalized). "
            "Inspect it as root: %s\n%s"
            % (initrd_root_ssh_command("journalctl -u yolobox-luks-migrate"), stderr)
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


def deadline_after(
    deadline: Optional[float],
    status: Optional[str],
    lima_is_starting: bool,
    now: float,
) -> Optional[float]:
    """The unlock budget after seeing lima's status: it starts when lima says Running."""
    if status == RUNNING:
        return deadline or now + INITRD_WAIT_SECONDS
    if lima_is_starting and status in LIMA_PRE_BOOT_STATUSES:
        # Lima is still creating, downloading or booting.
        return None
    raise vm_lost_error(status, "its initrd to take the disk passphrase")


def vm_lost_error(status: Optional[str], waiting_for: str) -> YoError:
    """The VM is not Running while yo waits for it (None: no longer there)."""
    return YoError(
        "%s is %s, not Running, while yo waited for %s — %s"
        % (
            state.box.name,
            status or "not listed",
            waiting_for,
            state.driver().vm_lost_hint(status),
        )
    )


def initrd_timeout_error(key_was_refused: bool) -> YoError:
    if key_was_refused:
        return YoError(
            "the sshd on 127.0.0.1:%d refused %s's key (root in an initrd, %s on a "
            "stage 2) — if %s is in its initrd, reading the key from cidata failed; "
            "see docs/disk-encryption.md#recognising-failures"
            % (
                state.driver().ssh_port(),
                state.driver().name,
                release.host_user(),
                state.box.name,
            )
        )
    return YoError(
        "%s never offered its initrd sshd on 127.0.0.1:%d within %d s of %s "
        "reporting it Running — %s, %s"
        % (
            state.box.name,
            state.driver().ssh_port(),
            INITRD_WAIT_SECONDS,
            state.driver().name,
            state.driver().list_command(),
            state.driver().initrd_unseen_hint(),
        )
    )


def unlock_disk(passphrase: bytes, starter: Optional[subprocess.Popen] = None) -> bool:
    """Answer the initrd's passphrase ask; True when this boot migrated the disk."""
    migrated_any = False
    session_ran = False
    deadline: Optional[float] = None
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
        endpoint = sshconfig.initrd_endpoint()
        kind = endpoint_kind(endpoint)
        # An unmarked sshd is asked for the operator first only once a session
        # ran: switch-root may have landed in a stage 2 built before the mark.
        operator_asked = kind is Endpoint.STAGE2 or (
            kind is Endpoint.INITRD and session_ran
        )
        if operator_asked:
            outcome = sshd_answers(endpoint, release.host_user())
            if outcome is SshOutcome.ANSWERED:
                return migrated_any
            if outcome is SshOutcome.REFUSED:
                # A fresh instance's first plain boot writes the operator's key
                # moments after its sshd starts, so a refusal is retried.
                refused_since = refused_since or time.monotonic()
        if kind is Endpoint.INITRD:
            attempt = run_initrd_helper(passphrase)
            migrated_any = migrated_any or attempt.migrated
            verdict = judge_helper_run(attempt)
            if verdict in (Verdict.UNLOCKED, Verdict.DROPPED):
                for line in attempt.stderr.splitlines():
                    ui.err(ui.quoted_line(line))
            if verdict is Verdict.UNLOCKED:
                return migrated_any
            if verdict is Verdict.FAILED:
                raise YoError(helper_failure_message(attempt))
            if verdict is Verdict.DROPPED:
                # Switch-root, or a migration that outlived the connection. A
                # fresh budget, since the old one may have expired while the
                # session streamed.
                session_ran = True
                deadline = time.monotonic() + INITRD_WAIT_SECONDS
            if verdict is Verdict.REFUSED:
                # Root refused by an unmarked sshd: an initrd without its keys,
                # or a stage 2 built before the mark, which may admit the
                # operator by now, or moments from now.
                if not operator_asked and stage2_up(endpoint):
                    return migrated_any
                refused_since = refused_since or time.monotonic()

        starter_alive = starter is not None and starter.poll() is None
        if starter is not None and not starter_alive and not session_ran:
            if starter.returncode != 0:
                raise YoError(
                    "%s exited %d before %s offered its initrd sshd"
                    % (
                        state.driver().start_command,
                        starter.returncode,
                        state.box.name,
                    )
                )
            # Lima's own ssh worked, so this was a plain boot: stage 2 is up.
            if stage2_up(endpoint):
                return migrated_any
        deadline = deadline_after(
            deadline,
            state.driver().vm_status(),
            starter_alive and not session_ran,
            time.monotonic(),
        )
        if deadline is not None and time.monotonic() > deadline:
            raise initrd_timeout_error(refused_since is not None)
        time.sleep(INITRD_POLL_SECONDS)


def wait_for_operator() -> None:
    deadline = time.monotonic() + OPERATOR_WAIT_SECONDS
    endpoint = sshconfig.initrd_endpoint()
    while True:
        # Bounded by ssh's ConnectTimeout and ServerAliveInterval.
        outcome, stderr = probe_sshd(endpoint, release.host_user())
        if outcome is SshOutcome.ANSWERED:
            return
        # First the VM: one that went away or paused (the host's OOM killer, a
        # crash) also fails a probe in shapes of its own, a keepalive timeout.
        status = state.driver().vm_status()
        if status != RUNNING:
            raise vm_lost_error(status, "its sshd to accept the operator")
        if outcome in (SshOutcome.ERROR, SshOutcome.RAN):
            # Of no shape a boot explains: ssh failed in a way yo has no name
            # for, or it logged in and `true` failed.
            raise YoError(
                "ssh to %s as the operator failed%s"
                % (state.box.name, proc.said_suffix(ui.quoted_block(stderr)))
            )
        # Nothing listening yet, or a refused key, is retried: a fresh box
        # writes the operator's key moments after its sshd starts.
        if time.monotonic() > deadline:
            raise YoError(
                "the disk is unlocked but %s is not reachable as the operator — %s"
                % (state.box.name, state.yo_cmd("status"))
            )
        time.sleep(OPERATOR_POLL_SECONDS)
