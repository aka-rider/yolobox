"""Unlocking the encrypted root through the initrd's sshd and waiting for the operator's sshd."""

from __future__ import annotations

import enum
import io
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, NamedTuple, Optional, Union

from . import proc, release, sshconfig, state, ui
from .config import (
    FACTS,
    LOOPBACK,
    LUKS_DEVICE,
    LUKS_KEY_ITEM,
    SSH_UNREACHABLE,
    INITRD_SSH_BANNER_MARK,
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

# The script in the initrd that answers the disk's password asks, and the
# line it ends a finished unlock with.
INITRD_UNLOCK_HELPER = FACTS["initrd_unlock_helper"]
HELPER_UNLOCKED_LINE = FACTS["unlock_unlocked_line"]
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


class SshAttempt(NamedTuple):
    """One ssh attempt's outcome, and what ssh said: the words an ERROR is
    shown with."""

    outcome: SshOutcome
    said: str


def ssh_attempt(argv: List[str], user: str) -> SshAttempt:
    """One `true` over the unpinned channel, and ssh's stderr."""
    completed = proc.run(argv, stdin=b"", guest=user)
    if completed.returncode == 0:
        return SshAttempt(SshOutcome.ANSWERED, completed.stderr)
    return SshAttempt(
        classify_initrd_attempt(completed.returncode, completed.stderr),
        completed.stderr,
    )


def unpinned_argv(remote: str, facts: SshFacts, user: str) -> List[str]:
    """ssh to the box's port that pins nothing, for operator_argv() and
    root_argv() alone.

    Before the disk is unlocked only the initrd's sshd answers, with a host key
    it generated this boot, so nothing can be pinned and whoever holds the
    loopback port gets the session: the driver's verify_initrd_listener()
    proves it the VM's before every root login. No pinned role may be used
    here: accept-new would write the initrd's key into known_hosts, and every
    stage-2 connection after it would be refused."""
    return (
        ["ssh", "-F", "/dev/null", "-i", facts.identity_file]
        + sshconfig.ssh_opt("IdentitiesOnly", "yes")
        + sshconfig.ssh_opt("IdentityAgent", facts.identity_agent)
        + sshconfig.ssh_opt("User", user)
        + sshconfig.ssh_opt("Port", str(facts.port))
        + sshconfig.ssh_opt("ControlPath", "none")
        + sshconfig.ssh_opt("BatchMode", "yes")
        + sshconfig.ssh_opt(
            "ConnectTimeout", str(sshconfig.SSH_CONNECT_TIMEOUT_SECONDS)
        )
        + sshconfig.ssh_opt(
            "ServerAliveInterval", str(sshconfig.SSH_ALIVE_INTERVAL_SECONDS)
        )
        + sshconfig.ssh_opt("LogLevel", "ERROR")
        + sshconfig.FIRST_CONTACT_OPTS
        + [LOOPBACK, remote]
    )


def operator_argv(remote: str, facts: SshFacts) -> List[str]:
    """The operator's login over the unpinned channel, which any sshd may be
    asked for: the host account's name, never root's."""
    return unpinned_argv(remote, facts, release.host_user())


def probe_operator(endpoint: SshFacts) -> SshAttempt:
    """One `true` as the operator."""
    return ssh_attempt(operator_argv("true", endpoint), release.host_user())


class Endpoint(enum.Enum):
    """Which sshd the box's port offers, read from its banner before any login.

    The one classifier every login as root, and every passphrase, is gated on:
    both go only to an INITRD, whose banner carries its own word. Root is an
    invalid user on any stage 2, which OpenSSH penalises per source address,
    and every host connection shares the gateway's address; a passphrase sent
    to a stage 2 is a secret handed to the wrong sshd.
    """

    NONE = "none"  # no line came; Banner.said names what came instead
    STAGE2 = "stage2"  # the banner carries the stage-2 word
    INITRD = "initrd"  # the banner carries the initrd's word
    # An ssh banner with neither word: the seed image's stage 2, before any build.
    UNMARKED = "unmarked"
    # A line that is no ssh identification at all: something else holds the port.
    UNKNOWN = "unknown"


class Banner(NamedTuple):
    """Which sshd the port offers, and what it said: the line it sent, which
    UNKNOWN is shown with, or for NONE what the connection did instead."""

    kind: Endpoint
    said: str


class NoBanner(Exception):
    """The port sent no line, for the reason the message names."""


# RFC 4253 section 4.2: SSH-protoversion-softwareversion, then comments.
SSH_IDENTIFICATION_RE = re.compile(r"SSH-2\.0-\S+(?: .*)?")


def endpoint_kind(endpoint: SshFacts) -> Banner:
    try:
        line = ssh_banner(endpoint)
    except NoBanner as absent:
        return Banner(Endpoint.NONE, str(absent))
    return Banner(banner_kind(line), line)


def banner_kind(line: str) -> Endpoint:
    if not line:
        return Endpoint.NONE
    if not SSH_IDENTIFICATION_RE.fullmatch(line.rstrip("\r")):
        return Endpoint.UNKNOWN
    words = line.split()
    if STAGE2_SSH_BANNER_MARK in words:
        return Endpoint.STAGE2
    if INITRD_SSH_BANNER_MARK in words:
        return Endpoint.INITRD
    return Endpoint.UNMARKED


def unknown_banner_error(banner: Banner) -> YoError:
    return YoError(
        "the port of %s, 127.0.0.1:%d, answers %s, which is no ssh identification: "
        "something else holds it — %s"
        % (
            state.box.name,
            state.driver().ssh_facts().port,
            ui.shown(banner.said),
            state.hint(state.driver().hints.where_to_look),
        )
    )


class InitrdEndpoint:
    """The box's ssh endpoint, proved the VM's own initrd sshd: its listener
    checked as the VM's, then its banner read with the initrd's word, under the
    box lock. Only initrd_endpoint() makes one, and a root login takes nothing
    else (root_argv)."""

    def __init__(self, key: object, facts: SshFacts) -> None:
        if key is not INITRD_ENDPOINT_KEY:
            raise TypeError("only unlock.initrd_endpoint() makes an InitrdEndpoint")
        self.facts = facts


INITRD_ENDPOINT_KEY = object()


def initrd_endpoint(held: state.HeldBox) -> Union[InitrdEndpoint, Banner]:
    """The gate of every root login and every passphrase: the listener on the
    box's port is proved the VM's, then its banner is read, and only the
    initrd's word yields an InitrdEndpoint; any other banner comes back as it
    was read. The facts are read once, so the listener checked is the one ssh
    then dials."""
    facts = state.driver().ssh_facts()
    state.driver().verify_initrd_listener(facts.port)
    banner = endpoint_kind(facts)
    if banner.kind is Endpoint.INITRD:
        return InitrdEndpoint(INITRD_ENDPOINT_KEY, facts)
    return banner


def require_initrd(held: state.HeldBox) -> InitrdEndpoint:
    """initrd_endpoint() for a caller that has just read the initrd's banner:
    any other answer now is an error that shows it."""
    gate = initrd_endpoint(held)
    if not isinstance(gate, InitrdEndpoint):
        raise YoError(
            "cannot look inside %s's initrd: its port no longer offers the "
            "initrd's sshd (%s)" % (state.box.name, ui.shown(gate.said))
        )
    return gate


def root_argv(remote: str, initrd: InitrdEndpoint) -> List[str]:
    """The one root login: to a proved initrd, whose listener is proved the
    VM's again right before ssh dials it, since a port nothing held at the
    gate can be taken after it."""
    state.driver().verify_initrd_listener(initrd.facts.port)
    return unpinned_argv(remote, initrd.facts, "root")


class BootStage(enum.Enum):
    """Which sshd, if any, answers on the box's port."""

    STAGE2 = "stage2"  # the disk is unlocked and the operator can log in
    INITRD = "initrd"  # locked: waiting in the initrd for the passphrase
    NONE = "none"  # nothing answers, or a stage 2 refused the operator
    # The initrd's sshd answers and refused root, so yo cannot look inside: it
    # may wait for the passphrase or be encrypting the disk. On libvirt a dead
    # or locked 1Password looks exactly like this; on Lima it is an initrd
    # without its keys.
    UNKNOWN = "unknown"


class Stage(NamedTuple):
    """The boot stage, and for NONE and UNKNOWN the phrase they are shown with:
    what came instead of a banner, or what ssh said ("" when nothing did)."""

    kind: BootStage
    said: str


def ssh_banner(endpoint: SshFacts) -> str:
    """The identification line the sshd on the box's port sends before any
    login. Reading it authenticates as nobody. NoBanner names what came
    instead of a line: a refused or reset connection, silence until the
    deadline, or a close."""
    host, port = LOOPBACK, endpoint.port
    timeout = sshconfig.SSH_CONNECT_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    banner = b""
    # TCP may hand the line over in pieces, so read until its newline, within
    # the line's size cap and one deadline for the whole read. What came of a
    # line before the deadline or a reset is judged as it came; any other
    # failure to connect is the host's, and an error.
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
    except socket.timeout:
        if not banner:
            raise NoBanner("no ssh banner within %d s" % timeout)
    except (ConnectionRefusedError, ConnectionResetError) as failure:
        if not banner:
            raise NoBanner("no ssh banner: %s" % ui.os_error_reason(failure))
    except OSError as failure:
        raise YoError(
            "cannot read the ssh banner on %s:%d: %s"
            % (host, port, ui.os_error_reason(failure))
        )
    if not banner:
        raise NoBanner("no ssh banner: the connection closed before a line came")
    return banner.split(b"\n", 1)[0].decode("ascii", "replace")


def boot_stage(held: state.HeldBox) -> Stage:
    """Which sshd answers, read off its banner before any login: a port that
    sends none gets no login at all, root is offered only through
    initrd_endpoint(), and any other sshd is asked for the operator alone.
    Every probe goes through unpinned_argv(), which pins nothing: a pinned
    ssh against the initrd's per-boot host key would print REMOTE HOST
    IDENTIFICATION HAS CHANGED."""
    endpoint = state.driver().ssh_facts()
    banner = endpoint_kind(endpoint)
    if banner.kind is Endpoint.INITRD:
        gate = initrd_endpoint(held)
        if isinstance(gate, InitrdEndpoint):
            root = ssh_attempt(root_argv("true", gate), "root")
            if root.outcome is SshOutcome.ANSWERED:
                return Stage(BootStage.INITRD, "")
            return Stage(BootStage.UNKNOWN, ssh_words(root.said))
        banner = gate  # the port's sshd changed between the two reads
    return operator_stage(endpoint, banner)


def operator_stage(endpoint: SshFacts, banner: Banner) -> Stage:
    """The stage behind a banner without the initrd's word, which is asked for
    the operator alone."""
    if banner.kind is Endpoint.NONE:
        return Stage(BootStage.NONE, banner.said)
    if banner.kind is Endpoint.UNKNOWN:
        raise unknown_banner_error(banner)
    operator = probe_operator(endpoint)
    if operator.outcome is SshOutcome.ANSWERED:
        return Stage(BootStage.STAGE2, "")
    words = ssh_words(operator.said)
    return Stage(
        BootStage.NONE,
        "its sshd did not let the operator in" + ("; %s" % words if words else ""),
    )


ASK_PASSWORD_DIR = FACTS["ask_password_dir"]
# One command for the initrd's root shell, builtins only (no ls there): whether
# a password ask nobody has answered is waiting.
INITRD_ASK_PROBE = (
    'for f in %s/ask.*; do if [ -e "$f" ]; then echo ask=pending; exit 0; fi; done; '
    "echo ask=none" % ASK_PASSWORD_DIR
)


def initrd_ask_pending(gate: InitrdEndpoint) -> bool:
    """Whether the initrd waits for a passphrase nobody has answered yet: the
    one moment nothing is being written to the disk, so the box may be powered
    off. An initrd that cannot be asked is an error, never a verdict."""
    completed = proc.run(root_argv(INITRD_ASK_PROBE, gate), stdin=b"", guest="root")
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


def unknown_stage_message(stage: Stage) -> str:
    """What BootStage.UNKNOWN means, with what ssh said, and the one way out.
    It never offers a forced stop: a refused login proves nothing about the
    boot, and another yo may already have delivered the passphrase, so the
    initrd may be encrypting the disk."""
    return (
        "the initrd's sshd answers on 127.0.0.1:%d and refused root's key%s, so yo "
        "cannot tell whether %s waits for its passphrase or is encrypting its disk. "
        "Fix the login (%s), then run the command again"
        % (
            state.driver().ssh_facts().port,
            stage_said(stage),
            state.box.name,
            state.driver().hints.login_fix,
        )
    )


def ssh_words(said: str) -> str:
    """`ssh said: ...` for a Stage, or "" when ssh said nothing."""
    words = " ".join(said.split())
    return "ssh said: %s" % words if words else ""


def stage_said(stage: Stage) -> str:
    """` (...)` with the stage's phrase for a message, or nothing."""
    return " (%s)" % ui.shown(stage.said) if stage.said else ""


@dataclass(frozen=True)
class HelperRun:
    """One run of the initrd's yolobox-unlock over ssh."""

    returncode: int
    stderr: str
    streamed: bool  # the helper's stdout reached the terminal before the session ended
    last_line: str = ""  # the last line the helper printed on stdout


def run_initrd_helper(
    gate: InitrdEndpoint, passphrase: Callable[[], bytes]
) -> HelperRun:
    """Send the passphrase to the initrd's helper and stream what it prints.

    Under the box lock for the whole run, which the gate proves: `yo down`
    must never power the box off between the moment this answers the ask and
    the moment its probe can see that it did. The caller took the gate right
    before, so nothing but the VM's initrd may cause a 1Password prompt.
    """
    stderr_lines: List[str] = []

    def collect(source: io.BufferedReader) -> None:
        for raw in iter(source.readline, b""):
            stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\n"))

    streamed = False
    last_line = ""
    key = passphrase()
    helper = proc.spawn(
        root_argv(INITRD_UNLOCK_HELPER, gate),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with helper as child:
        collector = threading.Thread(target=collect, args=(child.stderr,))
        collector.start()
        try:
            child.stdin.write(key + b"\n")
            child.stdin.close()
        except BrokenPipeError:
            pass  # ssh already ended; its exit code and stderr, returned below, say why
        for raw in iter(child.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            streamed, last_line = True, line
            ui.out(ui.quoted_line(line))
        collector.join()
        returncode = child.wait()
    return HelperRun(returncode, "\n".join(stderr_lines), streamed, last_line)


class Verdict(enum.Enum):
    """What unlock_disk does after one helper run.

    UNLOCKED: the helper finished its job. FAILED: the helper said why it
    failed, which retrying cannot fix. DROPPED: the session ran and then ended
    (switch-root, or an encryption that outlived the connection). REFUSED: sshd
    refused the key, and stage 2's sshd may be about to accept it.
    NOT_LISTENING: nothing to talk to yet. ERROR: ssh failed in a way yo has no
    name for; the run's stderr says how.
    """

    UNLOCKED = "unlocked"
    FAILED = "failed"
    DROPPED = "dropped"
    REFUSED = "refused"
    NOT_LISTENING = "not-listening"
    ERROR = "error"


def judge_helper_run(attempt: HelperRun) -> Verdict:
    outcome = classify_initrd_attempt(attempt.returncode, attempt.stderr)
    if outcome is SshOutcome.RAN:
        if attempt.returncode != 0:
            return Verdict.FAILED
        # Exit 0 alone is also a helper cut short by a shell that exited 0.
        if attempt.last_line == HELPER_UNLOCKED_LINE:
            return Verdict.UNLOCKED
        return Verdict.ERROR
    if attempt.streamed:
        return Verdict.DROPPED
    if outcome is SshOutcome.ERROR:
        return Verdict.ERROR
    if outcome is SshOutcome.REFUSED:
        return Verdict.REFUSED
    return Verdict.NOT_LISTENING


def initrd_root_ssh_command(remote_command: str, initrd: InitrdEndpoint) -> str:
    """The command yo itself would run, for a printed remedy to paste: text,
    never run by yo, so its listener is the operator's to judge then."""
    return shlex.join(unpinned_argv(remote_command, initrd.facts, "root"))


def helper_failure_message(attempt: HelperRun, initrd: InitrdEndpoint) -> str:
    code, stderr = attempt.returncode, ui.quoted_block(attempt.stderr)
    if code == 0:
        return (
            "the initrd helper exited 0 without its last line saying '%s'; it "
            "last said '%s'%s"
            % (
                HELPER_UNLOCKED_LINE,
                ui.shown(attempt.last_line),
                "\n" + stderr if stderr else "",
            )
        )
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
            % (initrd_root_ssh_command("journalctl -b", initrd), stderr)
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


class Round(NamedTuple):
    """What one round of unlock_disk came to, for the message its deadline ends
    with: a phrase true for the sshd it met, and the way out of that state."""

    what: str
    way: str


def where_to_look() -> str:
    return state.hint(state.driver().hints.where_to_look)


def ssh_round(what: str, attempt: SshAttempt, way_if_refused: str) -> Round:
    refused = attempt.outcome is SshOutcome.REFUSED
    return Round(
        "%s: %s%s" % (what, attempt.outcome.value, ssh_quoted(attempt.said)),
        way_if_refused if refused else where_to_look(),
    )


def ssh_quoted(said: str) -> str:
    """` (ssh said: ...)` for a message, or nothing when ssh said nothing."""
    words = ssh_words(said)
    return " (%s)" % ui.shown(words) if words else ""


def operator_round(kind: Endpoint, attempt: SshAttempt) -> Round:
    """A round that asked a banner without the initrd's word for the operator."""
    if kind is Endpoint.STAGE2:
        return ssh_round(
            "its stage-2 sshd sent its banner and was asked for the operator",
            attempt,
            "fix %s" % state.driver().hints.login_fix,
        )
    # The seed image's stage 2 admits the operator once cidata wrote the key.
    return ssh_round(
        "its unmarked sshd (the seed image's stage 2) sent its banner and was "
        "asked for the operator",
        attempt,
        "an unmarked sshd that keeps refusing the operator is a seed image whose "
        "key never landed; recreate it: %s" % state.driver().recreate_command(NO_SIZE),
    )


def initrd_timeout_error(last: Round) -> YoError:
    """The deadline of unlock_disk, quoting its last round."""
    return YoError(
        "%s did not let the operator in within %d s of %s reporting it Running; "
        "the last attempt on 127.0.0.1:%d: %s — %s"
        % (
            state.box.name,
            INITRD_WAIT_SECONDS,
            state.driver().name,
            state.driver().ssh_facts().port,
            last.what,
            last.way,
        )
    )


def unlock_disk(held: state.HeldBox, passphrase: Callable[[], bytes]) -> None:
    """Answer the initrd's passphrase ask and wait until the boot is past it.

    The VM is Running when this starts: the driver's start() has seen to it.
    `passphrase` is called only for an sshd whose banner is the initrd's, so
    a plaintext root (the seed image's) boots without 1Password being asked.
    Every round that ends in no login is retried until the deadline, which
    quotes the last one."""
    deadline = time.monotonic() + INITRD_WAIT_SECONDS
    # Both outcomes are named: a seed image's disk is not encrypted yet, and its
    # boot ends here with no ask at all.
    ui.err(
        "yo: waiting for %s to boot: its initrd to ask for the disk passphrase, "
        "or its sshd to answer if the disk is not encrypted" % state.box.name
    )
    while True:
        # The banner decides what this round may do (Endpoint): the helper,
        # with the passphrase on its stdin as root, runs only against the
        # initrd's banner; any other sshd is asked for the operator and nothing
        # else, so neither a stage 2 nor the seed image ever sees root.
        endpoint = state.driver().ssh_facts()
        banner = endpoint_kind(endpoint)
        if banner.kind is Endpoint.UNKNOWN:
            raise unknown_banner_error(banner)
        if banner.kind is Endpoint.NONE:
            last = Round(banner.said or "no ssh banner", where_to_look())
        elif banner.kind is not Endpoint.INITRD:
            # A fresh instance's first plain boot writes the operator's key
            # moments after its sshd starts, so a refusal is retried too.
            operator = probe_operator(endpoint)
            if operator.outcome is SshOutcome.ANSWERED:
                return
            last = operator_round(banner.kind, operator)
        else:
            gate = initrd_endpoint(held)
            if not isinstance(gate, InitrdEndpoint):
                last = Round(
                    "its initrd's banner was gone at the gate", where_to_look()
                )
            else:
                attempt = run_initrd_helper(gate, passphrase)
                verdict = judge_helper_run(attempt)
                if verdict in (Verdict.UNLOCKED, Verdict.DROPPED):
                    for line in attempt.stderr.splitlines():
                        ui.err(ui.quoted_line(line))
                if verdict is Verdict.UNLOCKED:
                    return
                if verdict in (Verdict.FAILED, Verdict.ERROR):
                    raise YoError(helper_failure_message(attempt, gate))
                if verdict is Verdict.DROPPED:
                    # Switch-root, or an encryption that outlived the
                    # connection. A fresh budget, since the old one may have
                    # expired while the session streamed.
                    deadline = time.monotonic() + INITRD_WAIT_SECONDS
                # REFUSED: the initrd's keys are not in place yet.
                last = ssh_round(
                    "its initrd's sshd sent its banner and was asked for root",
                    SshAttempt(
                        classify_initrd_attempt(attempt.returncode, attempt.stderr),
                        attempt.stderr,
                    ),
                    "if %s is in its initrd, reading the key from cidata failed; "
                    "fix %s" % (state.box.name, state.driver().hints.login_fix),
                )

        status = vm_is_running()
        if status is None:
            raise vm_lost_error(
                state.driver().vm_status(), "its initrd to take the disk passphrase"
            )
        if time.monotonic() > deadline:
            raise initrd_timeout_error(last)
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
        outcome, stderr = probe_operator(endpoint)
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
