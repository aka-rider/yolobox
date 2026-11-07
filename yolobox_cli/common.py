"""Definitions shared with aws-broker: it imports this module without the rest of the package."""

from __future__ import annotations

import os
import re
import select
from typing import Callable, NamedTuple, Optional

# Guest-controlled text reaches yo's terminal output and aws-broker's log
# (ssh stderr, paths, helper output). A terminal acts on escape sequences and C1
# controls, so they are shown as "?". Tab and newline stay. A lone surrogate (an
# undecodable byte read with surrogateescape) is "?" too: a strict stdout cannot
# encode it, and the print would fail instead of the line showing.
CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

# A valid profile name must start with an alphanumeric, so it can never read as
# an option. yo filters the guest's profiles with it and the broker refuses any
# other name.
PROFILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
PROFILE_NAME_RE = re.compile(PROFILE_NAME_PATTERN)


def sanitize(text: str) -> str:
    return CONTROL_BYTES_RE.sub("?", text)


class PidExitWatch(NamedTuple):
    wait: Callable[[Optional[float]], bool]
    close: Callable[[], None]


def pid_exit_watch(pid: int) -> PidExitWatch:
    """Watch `pid` for its exit without waiting yet: a kqueue on macOS, a pidfd on
    Linux (os.pidfd_open: Python 3.9 and a 5.3 kernel).

    `wait(timeout)` blocks until the process has exited or `timeout` seconds
    passed (forever for None) and says whether it exited; `close()` releases the
    watch. Raises ProcessLookupError when the pid is not running and OSError when
    the watch cannot be set up.
    """
    if hasattr(select, "kqueue"):
        kq = select.kqueue()
        try:
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD,
                fflags=select.KQ_NOTE_EXIT,
            )
            kq.control([event], 0)
        except BaseException:
            kq.close()
            raise
        return PidExitWatch(
            lambda timeout=None: bool(kq.control(None, 1, timeout)), kq.close
        )
    if not hasattr(os, "pidfd_open"):
        raise OSError(
            f"cannot watch pid {pid}: this Python has neither select.kqueue nor "
            "os.pidfd_open (macOS, or Linux with a Python that offers pidfd_open)"
        )
    pidfd = os.pidfd_open(pid)
    return PidExitWatch(
        lambda timeout=None: bool(select.select([pidfd], [], [], timeout)[0]),
        lambda: os.close(pidfd),
    )
