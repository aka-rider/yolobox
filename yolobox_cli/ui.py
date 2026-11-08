"""What yo says: errors, the terminal-safe output functions and the check report."""

from __future__ import annotations

import sys
from typing import List, NamedTuple, Optional

from . import common


class YoError(Exception):
    def __init__(self, message: Optional[str] = None, code: int = 1):
        super().__init__(message or "")
        self.message = message
        self.code = code


class RunTimeout(YoError):
    """A command `run()` gave up on after its timeout."""


def out(line: str = "") -> None:
    print(common.sanitize(line), flush=True)


def err(line: str = "") -> None:
    print(common.sanitize(line), file=sys.stderr, flush=True)


SHOWN_MAX_CHARS = 500


def shown(text: str) -> str:
    """`text`, a value that came from the guest, as one printable line inside a
    message: a name, a unit state, what a probe answered.

    A plain value is returned as it is. One with a control character, a newline
    (which could start a forged message line), a bidi override or an undecodable
    byte gets python's backslash escapes, so the reader sees what it holds. One
    longer than SHOWN_MAX_CHARS is cut there, with a marker that says how long it
    was, so a guest cannot fill the terminal through a message that quotes it.
    out() and err() only replace control characters with an unreadable "?".
    """
    cut = text[:SHOWN_MAX_CHARS]
    display = cut if cut.isprintable() else repr(cut)[1:-1]
    if len(text) > SHOWN_MAX_CHARS:
        display += "[cut: %d characters in all]" % len(text)
    return display


# Every line a guest printed is shown behind this, so a reader (and a test) tells
# the box's words from yo's: no guest line can start a `yo:` or `FAIL:` line.
GUEST_LINE_PREFIX = "  | "
QUOTED_BLOCK_MAX_LINES = 40
QUOTED_BLOCK_MAX_CHARS = 4096


def quoted_line(line: str) -> str:
    """One line a guest printed (no newline in it), behind GUEST_LINE_PREFIX."""
    return GUEST_LINE_PREFIX + common.sanitize(line)


def quoted_block(text: str) -> str:
    """`text`, what a guest printed, as lines a message can carry: each prefixed with
    GUEST_LINE_PREFIX, control bytes replaced, cut after QUOTED_BLOCK_MAX_LINES lines
    or QUOTED_BLOCK_MAX_CHARS characters with a line that says what the whole held.
    Empty text (or only whitespace) is an empty block, so `or "no output"` works."""
    clean = common.sanitize(text).strip()
    lines = clean.splitlines()
    block = "\n".join(lines[:QUOTED_BLOCK_MAX_LINES])[:QUOTED_BLOCK_MAX_CHARS]
    if len(block) < len(clean):
        block += "\n[cut: %d lines, %d characters in all]" % (len(lines), len(clean))
    return "\n".join(quoted_line(line) for line in block.splitlines())


def os_error_reason(failure: OSError) -> str:
    """Why the OS call failed, as one phrase for a message.

    `strerror` is None for an OSError raised without an errno (shutil.rmtree's
    symlink refusal, say), so the error's own arguments stand in for it.
    """
    return failure.strerror or " ".join(str(arg) for arg in failure.args)


class StatusLine(NamedTuple):
    """One line of a report, and whether it is a warning that fails the run."""

    text: str
    warning: bool = False


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.warnings: List[str] = []

    def warn(self, line: str) -> None:
        """Print a line that says something is wrong, and remember that it did."""
        self.warnings.append(line)
        out(line)

    def emit(self, line: StatusLine) -> None:
        if line.warning:
            self.warn(line.text)
        else:
            out(line.text)

    def ok(self, message: str) -> None:
        out("ok:   %s" % message)

    def fail(self, message: str, *remedies: str) -> None:
        self.failed = True
        err("FAIL: %s" % message)
        for remedy in remedies:
            err("      %s" % remedy)

    def abort(self, message: str, *remedies: str) -> "YoError":
        self.fail(message, *remedies)
        return YoError(None, code=1)
