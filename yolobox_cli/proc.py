"""Running and spawning processes, and small file and string helpers."""

from __future__ import annotations

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
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", errors="surrogateescape") as handle:
            handle.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def delete_path(path: str, delete: Callable[[str], None]) -> None:
    """Run `delete` on the path. One already gone is the wanted end state; any other
    failure is a YoError, whose text names the path that failed (a nested one, for a tree)."""
    try:
        delete(path)
    except FileNotFoundError:
        pass
    except OSError as failure:
        # A failure inside a tree carries the nested path that failed.
        reason = ui.os_error_reason(failure)
        where = "" if failure.filename in (None, path) else " at %s" % failure.filename
        raise YoError("cannot delete %s: %s%s" % (path, reason, where))


def delete_file(path: str) -> None:
    delete_path(path, os.remove)


def delete_tree(path: str) -> None:
    """Delete the directory and all in it, failing like delete_file."""
    delete_path(path, rmtree_naming_the_failed_path)


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
) -> subprocess.CompletedProcess:
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
    try:
        if filtered:
            completed = run_filtered(argv)
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
    if capture:
        completed.stdout = completed.stdout or b""
        if not binary:
            completed.stdout = completed.stdout.decode("utf-8", "replace").replace(
                "\r", ""
            )
        if completed.stderr is not None:
            completed.stderr = completed.stderr.decode("utf-8", "replace").replace(
                "\r", ""
            )
    if check and completed.returncode != 0:
        detail = failure_detail(completed) if capture else ""
        raise YoError(
            "%s failed (exit %d)%s"
            % (shlex.join(argv), completed.returncode, ": " + detail if detail else "")
        )
    return completed


def failure_detail(completed: subprocess.CompletedProcess) -> str:
    """What a failed process said: its stderr, else its stdout (bytes from a binary run included)."""
    stdout = completed.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    return (completed.stderr or stdout or "").strip()


def spawn(argv: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen:
    """Start a process that outlives the call; a missing program is a YoError."""
    try:
        return subprocess.Popen(list(argv), **popen_kwargs)
    except FileNotFoundError:
        raise YoError("%s not found on PATH" % argv[0])


def run_filtered(argv: Sequence[str]) -> subprocess.CompletedProcess:
    # Streams the output live, line by line, through out() and err(), so guest
    # text (a project path, a Makefile's recipe) is filtered like any other.
    def pump(source: io.BufferedReader, sink: Callable[[str], None]) -> None:
        for raw in iter(source.readline, b""):
            sink(raw.decode("utf-8", "replace").rstrip("\n"))

    with subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        errors = threading.Thread(target=pump, args=(child.stderr, ui.err))
        errors.start()
        pump(child.stdout, ui.out)
        errors.join()
    return subprocess.CompletedProcess(list(argv), child.returncode)


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
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def is_ascii_number(text: str) -> bool:
    """Whether text is only the digits 0-9 (str.isdigit also admits "²" and "１２")."""
    return text.isascii() and text.isdigit()
