"""The ssh roles and the one place that builds an ssh command line for the guest."""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import proc, sshconfig, state, ui
from .config import AGENT_USER, SSH_UNREACHABLE, VERSION_STAMP
from .driver import Removal
from .sshconfig import FIRST_CONTACT_OPTS
from .ui import YoError


@dataclass(frozen=True)
class SshRole:
    user: Optional[str]
    own_control_path: bool
    pins_host_key: bool = True

    @property
    def name(self) -> str:
        """The account the role logs in as, for a message: "operator" or the agent's."""
        return self.user or "operator"


OPERATOR = SshRole(None, False)
AGENT = SshRole(AGENT_USER, True)
# Only for the first fetch of the guest's host keys (host_key_fetch_role):
# with nothing pinned yet there is nothing to check a key against.
OPERATOR_FIRST_CONTACT = SshRole(None, False, pins_host_key=False)


def host_key_fetch_role() -> SshRole:
    """The role ensure_guest_known_hosts fetches the guest's host keys with.

    First contact only while nothing is pinned: a fresh instance, or a file
    emptied by hand. Once a pin exists the fetch is pinned too, so a key that
    changed under a kept known_hosts (a VM recreated behind yo's back, or
    something else holding the port) is ssh's own REMOTE HOST IDENTIFICATION
    HAS CHANGED refusal, never learned anew and then trusted with the agent
    forward. The way out of a stale pin is to delete the file.
    """
    pin = proc.read_text_file(state.known_hosts_path(), errors="surrogateescape")
    if pin is None or not pin.strip():
        return OPERATOR_FIRST_CONTACT
    return OPERATOR


def ssh_options(role: SshRole, mux: bool = False) -> List[str]:
    """The options every ssh and scp that yo spawns for the guest starts with."""
    argv = ["-F", state.ssh_cfg()]
    if not role.pins_host_key:
        argv += FIRST_CONTACT_OPTS
    if role.user is not None:
        argv += sshconfig.ssh_opt("User", role.user)
    if not mux:
        argv += sshconfig.ssh_opt("ControlPath", "none")
    elif role.own_control_path:
        argv += sshconfig.ssh_opt("ControlPath", state.agent_control_path())
    return argv


# Before every command yo runs in the guest: yo reads the words and the order
# of what it prints (lsblk, df, ssh-add, sort), which the guest's locale would
# translate. An interactive session keeps the operator's locale.
GUEST_C_LOCALE = ["env", "LC_ALL=C"]


def guest_argv(
    role: SshRole,
    argv: Sequence[str],
    *,
    stdin: bool,
    mux: bool = False,
    ssh_log: Optional[str] = None,
) -> List[str]:
    """`ssh` running `argv` on the box under the C locale: the one way a
    command yo reads reaches the guest. Without `stdin`, ssh's stdin is
    closed (-n). With `ssh_log`, ssh writes its own messages to that file
    (-E), apart from the guest's stderr, which the guest controls."""
    return (
        ["ssh"]
        + ssh_options(role, mux)
        + ([] if stdin else ["-n"])
        + ([] if ssh_log is None else ["-E", ssh_log])
        + [state.box.vm_host, shlex.join(GUEST_C_LOCALE + list(argv))]
    )


def interactive_argv(
    role: SshRole, options: Sequence[str], remote: Sequence[str]
) -> List[str]:
    """`ssh` for a session the operator types into: `remote` reaches the guest's
    shell as given, in the operator's locale."""
    return ["ssh"] + ssh_options(role) + list(options) + [state.box.vm_host, *remote]


def control_argv(role: SshRole, action: str, *spec: str) -> List[str]:
    """`ssh -O action` to the role's master: no command reaches the guest."""
    return (
        ["ssh"] + ssh_options(role, mux=True) + ["-O", action, *spec, state.box.vm_host]
    )


# What `ssh -O check` prints: the master's pid when one answers, and, when
# none does, the failed connect to its socket (no socket, or a stale one).
MASTER_PID_RE = re.compile(r"Master running \(pid=(\d+)\)")
NO_MASTER_MARK = "Control socket connect("


def master_pid(role: SshRole) -> Optional[int]:
    """The pid of the role's ssh master, or None when ssh says none answers its
    socket. Any other answer is an error that shows it: an exit code alone
    cannot tell a missing master from an ssh that failed for another reason."""
    argv = control_argv(role, "check")
    check = proc.run(argv)
    said = check.stderr + check.stdout
    found = MASTER_PID_RE.search(said)
    if check.returncode == 0 and found is not None:
        return int(found.group(1))
    if check.returncode == SSH_UNREACHABLE and NO_MASTER_MARK in said:
        return None
    raise YoError(proc.failed_message(shlex.join(argv), check.returncode, said.strip()))


def master_alive(role: SshRole) -> bool:
    return master_pid(role) is not None


def stop_master(role: SshRole, failure: str) -> None:
    """Ask the role's ssh master to quit, and raise `failure` (then ssh's stderr)
    when one still answers. What counts is that none is left: a master that died
    on its own since the caller looked makes the exit fail, and that is fine."""
    exit_run = proc.run(control_argv(role, "exit"))
    if master_alive(role):
        said = ui.quoted_block(exit_run.stderr)
        raise YoError(
            "%s:\n%s" % (failure, said) if said else "%s (ssh said nothing)" % failure
        )


def agent_master_removals() -> List[Removal]:
    """Closing the agent account's ssh master, if one runs. The agent role's mux
    sessions open it (ControlMaster auto, ControlPersist yes) on both drivers,
    at a socket inside the instance directory, so it must quit before that
    directory goes."""
    if not master_alive(AGENT):
        return []

    def close(held: state.HeldBox) -> None:
        stop_master(
            AGENT,
            "%s's agent ssh master is still running after `ssh -O exit`"
            % state.box.name,
        )

    return [Removal("close the agent's ssh master", close)]


def forward_agent_opts() -> List[str]:
    """`yo ssh`'s forward of 1Password's agent. ssh requests a forward only
    while it holds an agent itself, and the ssh config gives the Lima roles
    IdentityAgent none, so the forwarded agent is the identity agent too;
    IdentitiesOnly in the config keeps the login on the instance's own key."""
    sock = state.driver().op_sock()
    return sshconfig.ssh_opt("ForwardAgent", sock) + sshconfig.ssh_opt(
        "IdentityAgent", sock
    )


def ssh_run(
    role: SshRole,
    argv: Sequence[str],
    *,
    stdin: Optional[bytes] = None,
    env: Optional[Dict[str, str]] = None,
    mux: bool = False,
    capture: bool = True,
    check: bool = False,
    merge_stderr: bool = False,
    binary: bool = False,
    filtered: bool = False,
    ssh_log: Optional[str] = None,
) -> subprocess.CompletedProcess:
    return proc.run(
        guest_argv(role, argv, stdin=stdin is not None, mux=mux, ssh_log=ssh_log),
        capture=capture,
        check=check,
        stdin=stdin,
        env=env,
        merge_stderr=merge_stderr,
        binary=binary,
        filtered=filtered,
        guest=role.name,
    )


class Unreachable(YoError):
    """ssh could not reach the box (exit 255), as opposed to a command that failed there."""


def unreachable(role: SshRole, said: Optional[str] = None) -> Unreachable:
    """The role's session exited 255, with what ssh printed (`said`); None when
    its stderr was streamed to the terminal instead. The message says whose
    session and which code, since a command the agent runs on the box can exit
    255 too: the reader can then tell the two apart."""
    message = state.not_up_hint(
        "cannot reach %s as %s (the %s's ssh session exited %d, ssh's own code for "
        "no connection)" % (state.box.name, role.name, role.name, SSH_UNREACHABLE)
    )
    block = ui.quoted_block(said or "")
    return Unreachable("%s\n%s" % (message, block) if block else message)


def probe(role: SshRole, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """ssh_run for a caller that reads the exit code as the command's. ssh's own
    255 is raised as Unreachable first. ssh says why whenever it fails, so a 255
    with an empty stderr is the command's own: it exited 255, or a signal ended it."""
    completed = ssh_run(role, argv, **kwargs)
    if completed.returncode == SSH_UNREACHABLE and completed.stderr != "":
        raise unreachable(role, completed.stderr)
    return completed


def guest_out(argv: Sequence[str], **kwargs) -> str:
    """What a command printed on the box. Unreachable when the box is lost, YoError with
    the command's stderr when it failed there: its output is a fact only if it succeeded."""
    return proc.checked(
        probe(OPERATOR, argv, **kwargs), shlex.join(argv), "on the box"
    ).stdout


# What an exists_probe prints: one of the two words, nothing else.
PRESENT, ABSENT = "present", "absent"


def exists_probe(path: str) -> List[str]:
    """A guest command that names whether `path` exists: a word, because an
    exit code alone is also a login shell, sudo or ssh failing."""
    return [
        "sh",
        "-c",
        'if [ -e "$1" ]; then echo %s; else echo %s; fi' % (PRESENT, ABSENT),
        "sh",
        path,
    ]


STAMP_PROBE = exists_probe(VERSION_STAMP)


def guest_path_exists(role: SshRole, path: str, *, mux: bool) -> bool:
    """Whether `path` exists on the box, as `role` sees it. The answer is a
    word on stdout, so a session that fails is an error, never a verdict.
    Unreachable when the role cannot log in."""
    argv = exists_probe(path)
    said = proc.checked(
        probe(role, argv, mux=mux), shlex.join(argv), "on the box"
    ).stdout.strip()
    if said not in (PRESENT, ABSENT):
        raise YoError(
            "cannot tell whether %s exists on %s: asked as %s, the box answered '%s'"
            % (path, state.box.name, role.name, ui.shown(said))
        )
    return said == PRESENT


def bootstrapped(*, mux: bool) -> bool:
    """Whether the box carries the version stamp its build writes: the box's
    own declaration that `yo bootstrap` built it. The seed image has none.
    With `mux` the probe opens, or rides, the operator's master: for a caller
    about to declare the services over it anyway."""
    return guest_path_exists(OPERATOR, VERSION_STAMP, mux=mux)


def not_bootstrapped_message() -> str:
    return (
        "%s is not bootstrapped yet: it has no agent account, and nothing is "
        "forwarded into it; build it with: %s"
        % (state.box.name, state.yo_cmd("bootstrap"))
    )


# What the agent's login prints once its session ran: the proof sshd let it in.
AGENT_LOGIN_WORD = "yolobox-agent-login"
# ssh's own line when sshd refused every key it offered (OpenSSH's
# sshconnect2.c: "%s@%s: Permission denied (%s)."). It is judged only in
# ssh's log (-E), which the guest cannot write: the agent's rc files can print
# the same words on the session's stderr and exit 255.
SSH_KEY_REFUSED_RE = re.compile(r"[^@\s]+@\S+: Permission denied \([a-z,-]+\)\.")


def broken_agent_login() -> Optional[str]:
    """Why a bootstrapped box's agent account cannot be used, or None when its
    login runs a command; Unreachable when ssh cannot reach the box at all.
    The caller knows the box is bootstrapped, or asks only once this fails.

    Each failure is named by whose words say it, kept apart by ssh's -E: in
    ssh's own log, sshd refusing the key is the account's, which `yo
    bootstrap` declares again, and any other line beside ssh's 255 is the box
    out of reach; with nothing there, the login ran, and the agent's shell
    failed it."""
    with tempfile.NamedTemporaryFile(prefix="yo-ssh-", suffix=".log") as log:
        completed = ssh_run(AGENT, ["echo", AGENT_LOGIN_WORD], ssh_log=log.name)
        ssh_said = log.read().decode("utf-8", "replace")
    if completed.returncode == 0 and completed.stdout.strip() == AGENT_LOGIN_WORD:
        return None
    if completed.returncode == SSH_UNREACHABLE and ssh_said.strip():
        refused = any(
            SSH_KEY_REFUSED_RE.fullmatch(line.strip()) for line in ssh_said.splitlines()
        )
        if not refused:
            raise unreachable(AGENT, ssh_said)
        return (
            "%s is bootstrapped, but its sshd refuses the agent's login; run %s, "
            "which declares the account again: %s"
            % (state.box.name, state.yo_cmd("bootstrap"), ssh_said.strip())
        )
    return (
        "%s is bootstrapped, but the agent's login shell does not run `echo %s`: "
        "the agent's own rc files, or the box's build; look with %s, or rebuild "
        "with %s\nits session exited %d%s"
        % (
            state.box.name,
            AGENT_LOGIN_WORD,
            state.yo_cmd("ssh"),
            state.yo_cmd("bootstrap"),
            completed.returncode,
            proc.what_it_said(completed),
        )
    )


def require_agent_account() -> None:
    """The gate of every command that needs the agent. The stamp is read only
    when the login fails, to name why: the seed image has no agent account,
    and a bootstrapped box whose agent cannot log in is broken. A refused
    agent is ssh's 255 like a lost box; reading the stamp is the operator's
    login, which raises Unreachable when the box is gone."""
    problem = broken_agent_login()
    if problem is not None:
        raise YoError(
            problem if bootstrapped(mux=False) else not_bootstrapped_message()
        )
