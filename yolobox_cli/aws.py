"""The AWS credential broker yo runs on the host and the files it pushes to the guest."""

from __future__ import annotations

import configparser
import enum
import http.client
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import common, proc, sshx, state, ui
from .config import AGENT_HOME, FILE_MODE_PRIVATE, HELPER, SCRIPT_DIR
from .sshx import AGENT
from .ui import YoError


GUEST_AWS_CONFIG_PATH = AGENT_HOME + "/.config/yolobox/aws-config"

# The broker's READY line, or its ERROR=..., arrives within this long of its start.
BROKER_HANDSHAKE_SECONDS = 30
# One read of the handshake pipe: far more than the one short line it carries.
HANDSHAKE_READ_BYTES = 4096
# A stale broker exits at SIGTERM as soon as it has closed its socket.
STALE_BROKER_EXIT_SECONDS = 10.0
# A /health round trip over a unix socket; a broker that needs longer is not healthy.
HEALTH_TIMEOUT_SECONDS = 2.0


class Handshake(NamedTuple):
    lines: List[str]
    closed: bool  # the broker closed its stdout: it has exited, or is about to


def read_handshake(child: subprocess.Popen, deadline: float) -> Handshake:
    fd = child.stdout.fileno()
    buffer = b""
    closed = False
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        # Past the deadline, one last poll: what the broker already wrote, or its
        # closed stdout, is read whatever the clock says, but nothing is waited for.
        ready, _, _ = select.select([fd], [], [], max(0.0, remaining))
        if not ready:
            break
        chunk = os.read(fd, HANDSHAKE_READ_BYTES)
        if not chunk:
            closed = True
            break
        buffer += chunk
        if remaining <= 0:
            break
    return Handshake(buffer.decode("utf-8", "replace").splitlines(), closed)


def checked_pid(value: object) -> Optional[int]:
    # JSON gives us whatever the other side wrote. pid 0 or 1 would make os.kill
    # act on a process group or on init; True is an int equal to 1, so > 1 also
    # refuses it.
    if isinstance(value, int) and value > 1:
        return value
    return None


def aws_broker_sock() -> str:
    """The socket Lima forwards into the guest."""
    return os.path.join(state.state_dir(), "aws-broker.sock")


def aws_broker_ctl_sock() -> str:
    """The broker's host-only socket, for yo's /health. Never forwarded."""
    return os.path.join(state.state_dir(), "aws-broker.ctl.sock")


AWS_REGION_RE = re.compile(r"[a-z]{2,4}(-[a-z]+)+-\d+", re.ASCII)


def aws_config_path() -> str:
    """The AWS config file the host's `aws` reads, so the one yo allow-lists from.

    The broker inherits yo's environment, so its `aws configure
    export-credentials` honours AWS_CONFIG_FILE; the allowlist must come from
    the same file or the two disagree on which profiles exist. An empty
    variable is the default, as the CLI treats it.
    """
    configured = os.environ.get("AWS_CONFIG_FILE")
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(state.home(), ".aws", "config")


def aws_allowlist() -> List[Tuple[str, str]]:
    config_path = aws_config_path()
    shown = state.tilde(config_path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # parser.read() skips a file it cannot open, which would read an
        # unreadable config as "no SSO profiles" and tear the broker down.
        try:
            with open(config_path) as handle:
                parser.read_file(handle)
        except FileNotFoundError:
            return []
        except OSError as failure:
            raise YoError(
                "cannot read %s: %s" % (config_path, ui.os_error_reason(failure))
            )
        # profile name -> its section. [default] and [profile default] both name
        # "default"; botocore's build_profile_map lets the later section win,
        # and the broker's `aws configure export-credentials` follows botocore.
        defined_by: Dict[str, str] = {}
        for section in parser.sections():
            if section == "default":
                name = "default"
            elif section.startswith("profile "):
                name = section[len("profile ") :]
            else:
                continue
            if not common.PROFILE_NAME_RE.fullmatch(name):
                ui.err(
                    "yo: profile '%s' has a name yo will not pass to the guest — excluded from the guest AWS config"
                    % name
                )
                continue
            if name in defined_by:
                ui.err(
                    "yo: [%s] and [%s] in %s both name the profile '%s' — "
                    "using [%s], ignoring the earlier one"
                    % (defined_by[name], section, shown, name, section)
                )
            defined_by[name] = section
        allowed: List[Tuple[str, str]] = []
        for name, section in defined_by.items():
            if not (
                parser.has_option(section, "sso_session")
                or parser.has_option(section, "sso_start_url")
            ):
                continue
            region = parser.get(section, "region", fallback="")
            if not region:
                ui.err(
                    "yo: profile '%s' has no region in %s — excluded from the guest AWS config"
                    % (name, shown)
                )
                continue
            if not AWS_REGION_RE.fullmatch(region):
                ui.err(
                    "yo: profile '%s' has region %r in %s, which is not an AWS region name — excluded from the guest AWS config"
                    % (name, region, shown)
                )
                continue
            allowed.append((name, region))
    except configparser.Error as failure:
        raise YoError("cannot parse %s: %s" % (config_path, failure))
    except UnicodeDecodeError as failure:
        raise YoError("cannot read %s: %s" % (config_path, failure))
    return allowed


def guest_aws_config_text(allowed: Sequence[Tuple[str, str]]) -> str:
    return "".join(
        "[profile %s]\nregion = %s\ncredential_process = %s aws-creds %s\n"
        % (name, region, HELPER, name)
        for name, region in allowed
    )


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    # http.client has no unix-socket transport of its own; overriding connect()
    # to dial an AF_UNIX socket instead of AF_INET is the documented way to add one.
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.unix_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class BrokerState(enum.Enum):
    UNREACHABLE = "unreachable"
    HEALTHY = "healthy"
    # Answers only on the socket forwarded to the guest: a broker started by an
    # older yo, which has no control socket. It is stopped by the pid it reports.
    NO_CONTROL = "no control socket"


class BrokerHealth(NamedTuple):
    state: BrokerState
    info: Dict[str, Any]  # the broker's /health JSON; empty when UNREACHABLE
    sock: Optional[str]  # the socket that answered; None when UNREACHABLE


BROKER_UNREACHABLE = BrokerHealth(BrokerState.UNREACHABLE, {}, None)
HTTP_OK = 200


def health_on(sock_path: str, timeout: float) -> Optional[Dict[str, Any]]:
    """The /health JSON the broker answers on `sock_path`, None for anything else."""
    try:
        conn = UnixSocketHTTPConnection(sock_path, timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            data = response.read()
        finally:
            conn.close()
        if response.status != HTTP_OK:
            return None
        info = json.loads(data.decode())
        return info if isinstance(info, dict) else None
    except (OSError, http.client.HTTPException, ValueError):
        return None


def aws_broker_health(timeout: float = HEALTH_TIMEOUT_SECONDS) -> BrokerHealth:
    """Ask the broker on its control socket, which the guest cannot crowd out.

    A broker from an older yo has none, but still answers on the forwarded
    socket; that is NO_CONTROL, not healthy, so it gets replaced.
    """
    control = aws_broker_ctl_sock()
    info = health_on(control, timeout)
    if info is not None:
        return BrokerHealth(BrokerState.HEALTHY, info, control)
    forwarded = aws_broker_sock()
    info = health_on(forwarded, timeout)
    if info is not None:
        return BrokerHealth(BrokerState.NO_CONTROL, info, forwarded)
    return BROKER_UNREACHABLE


def pid_exits_within(pid: int, timeout: float) -> bool:
    """Whether `pid` has exited, or exits within `timeout` seconds."""
    try:
        watch = common.pid_exit_watch(pid)
    except ProcessLookupError:
        return True
    except OSError as failure:
        raise YoError("cannot watch pid %d for its exit: %s" % (pid, failure))
    try:
        return watch.wait(timeout)
    finally:
        watch.close()


def wait_for_pid_exit(pid: int, timeout: float) -> None:
    if not pid_exits_within(pid, timeout):
        raise YoError(
            "stale aws-broker (pid %d) did not exit within %gs" % (pid, timeout)
        )


def kill_stale_broker(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        raise YoError(
            "cannot signal the stale aws-broker (pid %d): permission denied" % pid
        )
    wait_for_pid_exit(pid, timeout=STALE_BROKER_EXIT_SECONDS)


def start_aws_broker(watch_pid: int, allowed_names: Sequence[str]) -> None:
    log_dir = os.path.join(state.state_dir(), "aws-broker")
    proc.private_dir(state.state_dir())
    proc.private_dir(log_dir)
    log_path = os.path.join(log_dir, "broker.log")
    # The previous broker's log is kept one generation: it holds why that
    # broker was replaced, and a fresh start must not erase that evidence.
    try:
        os.replace(log_path, log_path + ".1")
    except FileNotFoundError:
        pass
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE_PRIVATE)
    with os.fdopen(log_fd, "wb") as log:
        child = proc.spawn(
            [
                str(SCRIPT_DIR / "aws-broker"),
                "--socket",
                aws_broker_sock(),
                "--control-socket",
                aws_broker_ctl_sock(),
                "--watch-pid",
                str(watch_pid),
                "--allow",
                *allowed_names,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
            bufsize=0,
        )

    deadline = time.monotonic() + BROKER_HANDSHAKE_SECONDS
    handshake = read_handshake(child, deadline)
    child.stdout.close()
    line = handshake.lines[0] if handshake.lines else ""
    if line.startswith("ERROR="):
        child.wait()
        raise YoError("aws-broker failed: %s" % line.removeprefix("ERROR="))
    if line == "READY":
        return
    closed_silently = handshake.closed and not line
    if closed_silently:
        # EOF means every writer closed the pipe, not that the broker exited:
        # wait only as long as the handshake has left.
        try:
            code = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        else:
            raise YoError(
                "aws-broker %s before its handshake — see %s"
                % (describe_exit(code), log_path)
            )
    child.terminate()
    child.wait()
    if closed_silently:
        raise YoError(
            "aws-broker closed its output without exiting, so it sent no handshake "
            "— see %s" % log_path
        )
    if not line:
        raise YoError(
            "aws-broker produced no handshake within %ds — see %s"
            % (BROKER_HANDSHAKE_SECONDS, log_path)
        )
    raise YoError(
        "aws-broker produced an unexpected handshake %r — see %s" % (line, log_path)
    )


def describe_exit(code: int) -> str:
    """How a Popen return code reads in a message: negative is a signal's number."""
    if code >= 0:
        return "exited with code %d" % code
    try:
        return "was killed by %s" % signal.Signals(-code).name
    except ValueError:
        return "was killed by signal %d" % -code


def push_config_argv(config_path: str, text: str) -> List[str]:
    """`sh -c` that installs `text` as `config_path` whole or not at all.

    The text travels as an argument, not on stdin: a dropped ssh connection
    gives `cat` a clean EOF, which installed an empty config, or a failure,
    which left the temp file. An argument arrives whole or the command does
    not run. mktemp and mv make the write atomic for a credential_process
    reading the file, and the trap removes the temp file if printf or mv fail.
    """
    config_dir = os.path.dirname(config_path)
    script = (
        'mkdir -p "$1" && tmp=$(mktemp "$1/.aws-config.XXXXXX") && '
        "trap 'rm -f \"$tmp\"' EXIT && "
        'printf "%s" "$3" >"$tmp" && mv "$tmp" "$2"'
    )
    return ["sh", "-c", script, "sh", config_dir, config_path, text]


def push_aws_guest_files(allowed: Sequence[Tuple[str, str]]) -> None:
    sshx.ssh_run(
        AGENT,
        push_config_argv(GUEST_AWS_CONFIG_PATH, guest_aws_config_text(allowed)),
        check=True,
    )
    existing = sshx.ssh_run(AGENT, ["test", "-e", AGENT_HOME + "/.aws/config"])
    if existing.returncode == 0:
        ui.err(
            "yo: %s/.aws/config exists in the VM but AWS_CONFIG_FILE=%s hides it"
            % (AGENT_HOME, GUEST_AWS_CONFIG_PATH)
        )


def kill_broker_in(health: BrokerHealth) -> None:
    if health.state is BrokerState.UNREACHABLE:
        return
    stale_pid = checked_pid(health.info.get("pid"))
    if stale_pid is None:
        raise YoError(
            "the aws-broker on %s answered /health without a usable pid (%r), so yo "
            "cannot stop it — kill it by hand" % (health.sock, health.info.get("pid"))
        )
    kill_stale_broker(stale_pid)


class BrokerDisableError(YoError):
    """The broker could not be turned off. Recreating the ssh master does not cure it."""


# yo's own lock, not the broker's: the broker flocks <socket>.lock while it
# starts, and a yo holding that one would make every broker it starts fail.
YO_BROKER_LOCK_NAME = "aws-broker.yo.lock"


def disable_aws_broker() -> None:
    # The removal is under the lock too, so it cannot land between another
    # yo's start and its push and leave a broker running with no guest config.
    try:
        with state.host_state_lock(YO_BROKER_LOCK_NAME, wait=True):
            kill_broker_in(aws_broker_health())
            sshx.ssh_run(AGENT, ["rm", "-f", GUEST_AWS_CONFIG_PATH], check=True)
    except YoError as failure:
        raise BrokerDisableError(failure.message, failure.code)


def broker_needs_restart(
    health: BrokerHealth, watch_pid: int, allowed_names: Sequence[str]
) -> bool:
    if health.state is not BrokerState.HEALTHY:
        return True
    if checked_pid(health.info.get("watch_pid")) != watch_pid:
        return True
    if health.info.get("allow") != list(allowed_names):
        return True
    return False


def ensure_aws_broker() -> bool:
    # Reading the allowlist and the pid, checking, killing, starting and
    # pushing the guest config are one step under yo's lock. Two yos racing
    # through them would each see no broker, and the second to start would fail
    # on the first one's; one that read the pid or the config before the other
    # restarted the broker would then replace a good broker with a stale one,
    # or push a guest config that names other profiles than the broker serves.
    with state.host_state_lock(YO_BROKER_LOCK_NAME, wait=True):
        if shutil.which("aws") is None:
            why_not = "'aws' is not on this host's PATH"
            allowed: List[Tuple[str, str]] = []
        else:
            allowed = aws_allowlist()
            why_not = "no SSO profiles in %s" % state.tilde(aws_config_path())
        if allowed:
            pid = state.driver().keepalive_pid()
            if pid is None:
                raise YoError(state.driver().keepalive_pid_error())
            allowed_names = sorted(name for name, _ in allowed)
            health = aws_broker_health()
            if broker_needs_restart(health, pid, allowed_names):
                kill_broker_in(health)
                start_aws_broker(pid, allowed_names)
            push_aws_guest_files(allowed)
            return True
    ui.err("yo: %s — no AWS credentials broker started" % why_not)
    disable_aws_broker()
    return False
