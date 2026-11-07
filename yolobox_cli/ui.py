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


class GuestText(str):
    """What a guest process printed on one channel, tagged with the account whose
    ssh session printed it (`role`: "operator" or the agent's name).

    out() and err() refuse one, so a guest line that reads `yo: ...` never reaches
    the terminal as yo's own: a message turns it into display text first, with
    shown() (one escaped line) or quoted_block() (every line prefixed). Slicing,
    stripping and splitting keep the tag, so a trimmed copy is refused too; text
    built from it with `%` or `+` is the caller's own, and the caller's duty.
    """

    role: str

    def __new__(cls, text: str, role: str) -> "GuestText":
        tagged = str.__new__(cls, text)
        tagged.role = role
        return tagged

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        return GuestText(str.__getitem__(self, key), self.role)

    def strip(self, chars: Optional[str] = None) -> "GuestText":
        return GuestText(str.strip(self, chars), self.role)

    def lstrip(self, chars: Optional[str] = None) -> "GuestText":
        return GuestText(str.lstrip(self, chars), self.role)

    def rstrip(self, chars: Optional[str] = None) -> "GuestText":
        return GuestText(str.rstrip(self, chars), self.role)

    def split(self, sep: Optional[str] = None, maxsplit: int = -1) -> List["GuestText"]:  # type: ignore[override]
        return [GuestText(part, self.role) for part in str.split(self, sep, maxsplit)]

    def splitlines(self, keepends: bool = False) -> List["GuestText"]:  # type: ignore[override]
        return [GuestText(line, self.role) for line in str.splitlines(self, keepends)]


def refuse_guest_text(line: str) -> None:
    if isinstance(line, GuestText):
        raise TypeError(
            "text the %s's session printed reached the terminal unquoted: pass it "
            "through ui.shown() or ui.quoted_block() first" % line.role
        )


def out(line: str = "") -> None:
    refuse_guest_text(line)
    print(common.sanitize(line), flush=True)


def err(line: str = "") -> None:
    refuse_guest_text(line)
    print(common.sanitize(line), file=sys.stderr, flush=True)


def shown(text: str) -> str:
    """`text`, a name that came from the guest, as one printable line inside a message.

    A plain name is returned as it is. One with a control character, a newline
    (which could start a forged message line), a bidi override or an undecodable
    byte gets python's backslash escapes, so the reader sees what it holds.
    Messages that name a guest file go through this; out() and err() still
    replace control characters, but only as an unreadable "?".
    """
    if text.isprintable():
        return str(text)
    return repr(text)[1:-1]


# Every line a guest printed is shown behind this, so a reader (and a test) tells
# the box's words from yo's: no guest line can start a `yo:` or `FAIL:` line.
GUEST_LINE_PREFIX = "  | "
QUOTED_BLOCK_MAX_LINES = 40
QUOTED_BLOCK_MAX_CHARS = 4096


def quoted_line(line: str) -> str:
    """One line a guest printed (no newline in it), behind GUEST_LINE_PREFIX."""
    return GUEST_LINE_PREFIX + common.sanitize(str(line))


def quoted_block(text: str) -> str:
    """`text`, what a guest printed, as lines a message can carry: each prefixed with
    GUEST_LINE_PREFIX, control bytes replaced, cut after QUOTED_BLOCK_MAX_LINES lines
    or QUOTED_BLOCK_MAX_CHARS characters with a line that says what the whole held.
    Empty text (or only whitespace) is an empty block, so `or "no output"` works."""
    clean = common.sanitize(str(text)).strip()
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
