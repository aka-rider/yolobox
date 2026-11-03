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
