"""Running and spawning processes, and small file and string helpers."""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, Optional, Sequence

from . import ui
from .ui import RunTimeout, YoError


def read_text_file(
    path: str, missing: Optional[str] = None, errors: str = "strict"
) -> Optional[str]:
    """The file's text, or `missing` when there is no such file.

    Any other failure to read it is a YoError, so a file that exists but cannot
    be read is never taken for one that is absent.
    """
    try:
        with open(path, "r", errors=errors) as handle:
            return handle.read()
    except FileNotFoundError:
        return missing
    except OSError as failure:
        raise YoError("cannot read %s: %s" % (path, failure))


def atomic_write(path: str, content: str, mode: int) -> None:
    """Write `content` to `path` in one step: a temporary file beside it, synced
    to disk, renamed over it, and the directory synced so the new name survives
    a crash. A failure removes the temporary file and is a YoError naming `path`."""
    directory = os.path.dirname(path) or "."
    try:
        write_and_rename(directory, path, content, mode)
    except OSError as failure:
        raise YoError("cannot write %s: %s" % (path, ui.os_error_reason(failure)))


def write_and_rename(directory: str, path: str, content: str, mode: int) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", errors="surrogateescape") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        # The write's own error is the one to raise; the temporary file may
        # already be gone (an interrupt landing after the rename).
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def remove_file(path: str) -> None:
    """Delete the file; one already gone is the wanted end state, any other
    failure a YoError."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as failure:
        raise YoError("cannot delete %s: %s" % (path, ui.os_error_reason(failure)))


def delete_tree(path: str) -> None:
    """Delete the directory and all in it. One already gone is the wanted end
    state; any other failure is a YoError, whose text names the nested path that
    failed."""
    try:
        rmtree_naming_the_failed_path(path)
    except FileNotFoundError:
        pass
    except OSError as failure:
        # A failure inside a tree carries the nested path that failed.
        reason = ui.os_error_reason(failure)
        where = "" if failure.filename in (None, path) else " at %s" % failure.filename
        raise YoError("cannot delete %s: %s%s" % (path, reason, where))


def rmtree_naming_the_failed_path(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path)
        return

    # Python 3.9-3.11 raise from inside the tree with the bare entry name as the
    # error's filename; only onerror is told the full path, 3.12+ sets it.
    def name_the_path(
        _function: Callable[..., Any], failed_path: str, exc_info: Any
    ) -> None:
        failure = exc_info[1]
        failure.filename = failed_path
        raise failure

    shutil.rmtree(path, onerror=name_the_path)


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise YoError("%s not found (%s)" % (name, hint))
    return path


# A captured channel of a guest session holds at most this many bytes: the
# agent owns what a session prints, and an unbounded read would let `yes` fill
# the host's memory. Past it the session is killed and named.
GUEST_CAPTURE_LIMIT = 1 << 20
# A streamed guest line is read in pieces of at most this many bytes; a longer
# line continues as the next piece, each shown behind the guest prefix.
GUEST_LINE_LIMIT = 64 * 1024


class GuestRun(subprocess.CompletedProcess):
    """A finished guest session: what it printed is the guest's, so a message
    shows it only as a quoted block (failure_detail)."""


def run(
    argv: Sequence[str],
    *,
    capture: bool = True,
    check: bool = False,
    stdin: Optional[bytes] = None,
    env: Optional[Dict[str, str]] = None,
    merge_stderr: bool = False,
    binary: bool = False,
    filtered: bool = False,
    timeout: Optional[float] = None,
    guest: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run `argv` to completion. `guest` names the account whose session on the
    box this is ("operator", "root", the agent's name); None is a host tool. A
    guest session's captured channels are capped at GUEST_CAPTURE_LIMIT bytes,
    and its result is a GuestRun."""
    if filtered:
        unsupported = [
            name
            for name, given in (
                ("stdin", stdin is not None),
                ("env", env is not None),
                ("timeout", timeout is not None),
                ("merge_stderr", merge_stderr),
                ("binary", binary),
            )
            if given
        ]
        if unsupported:
            # A programming error, not an operator's: run_filtered streams the
            # child's output as it comes and would drop these without a word.
            raise ValueError(
                "run(filtered=True) cannot honour %s" % ", ".join(unsupported)
            )
    if guest is not None and timeout is not None:
        raise ValueError("run(guest=...) caps the session's output and has no timeout")
    try:
        if filtered:
            completed = run_filtered(argv)
        elif capture and guest is not None:
            completed = run_capped(argv, stdin, env, merge_stderr, guest)
        else:
            completed = subprocess.run(
                list(argv),
                input=stdin,
                stdout=subprocess.PIPE if capture else None,
                stderr=(subprocess.STDOUT if merge_stderr else subprocess.PIPE)
                if capture
                else None,
                env=env,
                timeout=timeout,
            )
    except FileNotFoundError:
        raise YoError("%s not found on PATH" % argv[0])
    except subprocess.TimeoutExpired:
        raise RunTimeout(
            "%s did not finish within %d s" % (shlex.join(argv), timeout or 0)
        )
    if capture and not filtered:
        completed.stdout = completed.stdout or b""
        if not binary:
            completed.stdout = decoded(completed.stdout)
        if completed.stderr is not None:
            completed.stderr = decoded(completed.stderr)
    if check:
        checked(completed, shlex.join(argv))
    return completed


def run_capped(
    argv: Sequence[str],
    stdin: Optional[bytes],
    env: Optional[Dict[str, str]],
    merge_stderr: bool,
    guest: str,
) -> GuestRun:
    """subprocess.run for a guest session that reads at most GUEST_CAPTURE_LIMIT
    bytes per channel. A channel that overflows kills the session, and the
    overflow is a YoError naming the account and the channel."""
    captured: Dict[str, bytes] = {}

    def read(name: str, source: io.BufferedReader, child: subprocess.Popen) -> None:
        captured[name] = source.read(GUEST_CAPTURE_LIMIT + 1)
        if len(captured[name]) > GUEST_CAPTURE_LIMIT:
            child.kill()

    with subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        env=env,
    ) as child:
        sources = {"stdout": child.stdout}
        if not merge_stderr:
            sources["stderr"] = child.stderr
        readers = [
            threading.Thread(target=read, args=(name, source, child))
            for name, source in sources.items()
        ]
        for reader in readers:
            reader.start()
        if stdin is not None:
            # A session that ended first breaks the pipe; its exit code and
            # stderr say why. close() closes the pipe even when its flush fails.
            with contextlib.suppress(BrokenPipeError):
                child.stdin.write(stdin)
            with contextlib.suppress(BrokenPipeError):
                child.stdin.close()
        for reader in readers:
            reader.join()
    for name, data in captured.items():
        if len(data) > GUEST_CAPTURE_LIMIT:
            raise YoError(
                "the %s's session printed more than %d bytes on %s, so it was "
                "killed: %s" % (guest, GUEST_CAPTURE_LIMIT, name, shlex.join(argv))
            )
    return GuestRun(
        list(argv), child.returncode, captured["stdout"], captured.get("stderr")
    )


def checked(
    completed: subprocess.CompletedProcess, what: str, where: str = ""
) -> subprocess.CompletedProcess:
    """`completed`, or the one YoError of a command that failed: `WHAT failed[
    WHERE] (exit N)` and what it said (nothing when its output went to the
    terminal). Every caller that reads an exit code and then gives up raises
    through here."""
    if completed.returncode == 0:
        return completed
    captured = completed.stdout is not None or completed.stderr is not None
    raise YoError(
        failed_message(
            what,
            completed.returncode,
            failure_detail(completed) if captured else None,
            where,
        )
    )


def decoded(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").replace("\r", "")


def failed_message(what: str, code: int, said: Optional[str], where: str = "") -> str:
    """The one spelling of a failed command: `WHAT failed[ WHERE] (exit N)`, then what
    it said (said_suffix). `said` is None when its output went to the terminal, so
    nothing follows."""
    return "%s failed%s (exit %d)%s" % (
        what,
        " " + where if where else "",
        code,
        "" if said is None else said_suffix(said),
    )


def said_suffix(said: str) -> str:
    """How a message ends in what a process said: `: SAID` on the same line, a
    quoted block (a guest session's words) on the lines after a colon, and an empty
    one said to be empty rather than ending the message on a bare colon."""
    if said.startswith(ui.GUEST_LINE_PREFIX):
        return ":\n" + said
    if said:
        return ": " + said
    return " with no output"


def what_it_said(completed: subprocess.CompletedProcess) -> str:
    """said_suffix of failure_detail: for a message that ends in what a failed
    process said."""
    return said_suffix(failure_detail(completed))


def failure_detail(completed: subprocess.CompletedProcess) -> str:
    """What a failed process said, for a message: its stderr, else its stdout (bytes
    from a binary run included). A guest session's words come back as a quoted
    block (ui.quoted_block), so they never pass for yo's own; a host tool's as
    they are, stripped."""
    stdout = completed.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    said = completed.stderr or stdout or ""
    if isinstance(completed, GuestRun):
        return ui.quoted_block(said)
    return said.strip()


def spawn(argv: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen:
    """Start a process that outlives the call; a missing program is a YoError."""
    try:
        return subprocess.Popen(list(argv), **popen_kwargs)
    except FileNotFoundError:
        raise YoError("%s not found on PATH" % argv[0])


def run_filtered(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Stream the guest session's output live, each line behind ui.GUEST_LINE_PREFIX
    through out() and err(), so a project path or a Makefile recipe it prints is
    filtered like any other guest text and can never pass for a line of yo's own.
    A line longer than GUEST_LINE_LIMIT bytes continues as the next. Nothing is
    captured: the result carries the exit code alone."""

    def pump(source: io.BufferedReader, sink: Callable[[str], None]) -> None:
        for raw in iter(lambda: source.readline(GUEST_LINE_LIMIT), b""):
            sink(ui.quoted_line(raw.decode("utf-8", "replace").rstrip("\n")))

    with subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        errors = threading.Thread(target=pump, args=(child.stderr, ui.err))
        errors.start()
        pump(child.stdout, ui.out)
        errors.join()
    return subprocess.CompletedProcess(list(argv), child.returncode, None, None)


def exec_process(argv: Sequence[str], env: Optional[Dict[str, str]] = None) -> None:
    # CPython starts with SIGPIPE ignored and exec preserves an ignored
    # disposition, so a pipeline reader that exits would leave the exec'd
    # process seeing EPIPE instead of dying, unlike bash's `exec ssh`.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        os.execvpe(argv[0], list(argv), env if env is not None else os.environ)
    except FileNotFoundError:
        raise YoError("%s not found on PATH" % argv[0])


def private_dir(path: str) -> None:
    """The directory, with its parents, readable by the owner alone."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as failure:
        raise YoError(
            "cannot make the directory %s: %s" % (path, ui.os_error_reason(failure))
        )


def is_ascii_number(text: str) -> bool:
    """Whether text is only the digits 0-9 (str.isdigit also admits "²" and "１２")."""
    return text.isascii() and text.isdigit()
