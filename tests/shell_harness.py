"""The harness the bash-with-fakes test modules share.

A script under test runs the way nix's writeShellApplication runs it, with
`set -o errexit -o nounset -o pipefail`, and finds a scratch `bin/` of fakes
first on PATH, ahead of the real tools it also needs. A fake is a small shell
script a test writes with `fake()`; it answers from FAKE_* variables in the
environment and logs what it was asked, so a test reads what the script did,
not how.

`facts_sh()` renders facts.json the way nix renders it into the box's
/etc/yolobox/facts.sh, for the scripts that source that file.

Every test module imports this one, so every test runs under a scratch $HOME
and none can reach the operator's real one: their 1Password agent socket,
~/.ssh, ~/.lima or ~/.local/state. Opening, listing, creating or connecting to
a path under the real home, from any thread of the test's own Python, fails
the test with RealHomeTouched; only the checkout and the running Python's own
files are read there. A subprocess is out of the audit hook's sight: bash
under ShellCase gets the scratch $HOME, and ssh, which takes ~ from the
account database whatever $HOME says, finds a guard first on PATH that
refuses to run without -F.

A test may not print: whatever it, the code under it or a child process
writes to stdout or stderr fails it, so a clean run shows unittest's own
output alone. A test that expects output captures it and checks it.
"""

import atexit
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FACTS = json.loads((REPO / "facts.json").read_text())
BASH = ["bash", "-o", "errexit", "-o", "nounset", "-o", "pipefail"]
# CI sets it to 1: its runner has every tool a test names, so a test that would
# skip although its tools are on PATH fails, because its probe is broken.
FORBID_SKIP = os.environ.get("YOLOBOX_FORBID_SKIP") == "1"
# Taken at import: a test may patch shutil.which to hide a tool from the code.
WHICH = shutil.which


class RealHomeTouched(BaseException):
    """A test reached a path under the operator's real home. A BaseException,
    so no `except Exception` in the code under test can turn it into a refusal
    the test was expecting."""


def real_homes():
    """The operator's home as the account database and $HOME name it."""
    homes = {pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME") or "/"}
    return sorted({os.path.realpath(home) for home in homes} - {"/"})


def under(path, roots):
    return any(path == root or path.startswith(root + os.sep) for root in roots)


REAL_HOMES = real_homes()
# Read under the real home on purpose: the checkout, and the running Python's
# stdlib, site-packages and bytecode cache (Xcode's caches in ~/Library). A
# home itself, or what holds it (a sys.path entry of ""), would exempt it all.
READ_UNDER_HOME = sorted(
    path
    for path in {
        os.path.realpath(entry)
        for entry in [str(REPO), *sys.path, sys.pycache_prefix]
        if entry
    }
    if not any(under(home, [path]) for home in REAL_HOMES)
)
AUDITED_PATH_EVENTS = {
    "open",
    "os.listdir",
    "os.scandir",
    "os.mkdir",
    "os.remove",
    "os.rmdir",
    "os.rename",
    "os.symlink",
    "os.chmod",
    "os.chown",
    "os.truncate",
    "os.utime",
    "shutil.rmtree",
    "socket.bind",
    "socket.connect",
}


def audited_paths(event, args):
    """The paths an audited event names, as strings; an fd or a TCP address
    names none."""
    if event.startswith("socket."):
        named = args[1:2]
    elif event in ("os.rename", "os.symlink"):
        named = args[:2]
    else:
        named = args[:1]
    for path in named:
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path):
            yield os.fsdecode(path)


def refuse_the_real_home(event, args):
    if event not in AUDITED_PATH_EVENTS:
        return
    for path in audited_paths(event, args):
        resolved = os.path.realpath(path)
        if under(resolved, REAL_HOMES) and not under(resolved, READ_UNDER_HOME):
            raise RealHomeTouched(
                "%s %s: a test reached the operator's real home; every test "
                "runs under the scratch HOME %s" % (event, path, SCRATCH_HOME)
            )


# Under /tmp, not the default temp dir: macOS's is /var/folders/xx/.../T, long
# enough that a ControlPath under $HOME passes the 104-byte unix socket limit.
SCRATCH_HOME = os.path.realpath(tempfile.mkdtemp(prefix="yo-home-", dir="/tmp"))
os.environ["HOME"] = SCRATCH_HOME
atexit.register(shutil.rmtree, SCRATCH_HOME)
sys.addaudithook(refuse_the_real_home)

# A non-interactive bash whose stdin is a socket takes itself for an ssh
# command and sources the system bashrc, which rebuilds PATH past every fake.
# No test reads the runner's stdin, so every child inherits /dev/null.
_devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(_devnull, 0)
os.close(_devnull)

# RealHomeTouched raised in a thread ends only that thread; it is kept here,
# and the test it ran under fails at its end.
THREAD_HOME_TOUCHES = []
THREAD_EXCEPTHOOK = threading.excepthook


def keep_thread_home_touches(args):
    if issubclass(args.exc_type, RealHomeTouched):
        THREAD_HOME_TOUCHES.append(str(args.exc_value))
    THREAD_EXCEPTHOOK(args)


threading.excepthook = keep_thread_home_touches


def fail_on_thread_home_touches():
    touched = THREAD_HOME_TOUCHES[:]
    THREAD_HOME_TOUCHES.clear()
    if touched:
        raise AssertionError("a thread reached the real home: " + "; ".join(touched))


# ssh reads ~/.ssh/config, known_hosts and identities from the home the
# account database names, not $HOME, unless told -F: a guard named ssh,
# first on PATH, runs the real one only with -F.
REAL_SSH = WHICH("ssh")
GUARD_BIN = os.path.join(SCRATCH_HOME, ".harness-bin")
if REAL_SSH is not None:
    os.mkdir(GUARD_BIN)
    guard = Path(GUARD_BIN, "ssh")
    guard.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do [ "$arg" = -F ] && exec %s "$@"; done\n'
        'echo "tests/shell_harness.py: ssh without -F reads the real ~/.ssh: $*" >&2\n'
        "exit 97\n" % shlex.quote(REAL_SSH)
    )
    guard.chmod(0o755)
    os.environ["PATH"] = GUARD_BIN + os.pathsep + os.environ.get("PATH", "")

# unittest's runner, made after the test modules are imported, writes to the
# sys.stderr of that moment: a descriptor of its own, so its output passes
# while a test's stdout and stderr are taken from it.
sys.stderr = open(
    os.dup(2),
    "w",
    buffering=1,
    encoding=sys.stderr.encoding,
    errors=sys.stderr.errors,
)


class OutputGuard:
    """Takes a test's stdout and stderr, the descriptors its child processes
    inherit and the Python streams alike, and fails the test with what it
    wrote, if anything."""

    def start(self):
        sys.stdout.flush()
        sys.stderr.flush()
        self.capture = tempfile.TemporaryFile()
        self.saved_fds = [os.dup(1), os.dup(2)]
        for fd in (1, 2):
            os.dup2(self.capture.fileno(), fd)
        self.saved_streams = sys.stdout, sys.stderr
        stream = open(
            self.capture.fileno(),
            "w",
            buffering=1,
            closefd=False,
            encoding=sys.stdout.encoding,
            errors=sys.stdout.errors,
        )
        sys.stdout = sys.stderr = self.stream = stream

    def check(self):
        self.stream.flush()
        sys.stdout, sys.stderr = self.saved_streams
        for fd, saved in zip((1, 2), self.saved_fds):
            os.dup2(saved, fd)
            os.close(saved)
        self.stream.close()
        self.capture.seek(0)
        said = self.capture.read().decode("utf-8", "backslashreplace")
        self.capture.close()
        if said:
            raise AssertionError(
                "the test printed, which a test must capture and check:\n" + said
            )


TEST_CASE_RUN = unittest.TestCase.run


def run_guarded(test, result=None):
    """TestCase.run with the output guard and the thread check around setUp,
    the test and every cleanup."""
    set_up = test.setUp

    def guarded_set_up():
        guard = OutputGuard()
        guard.start()
        test.addCleanup(guard.check)
        test.addCleanup(fail_on_thread_home_touches)
        set_up()

    test.setUp = guarded_set_up
    return TEST_CASE_RUN(test, result)


unittest.TestCase.run = run_guarded


def missing_tool_reason(tools, have=None, why=None):
    """The skip reason of a test that needs `tools` (a name or a list), or None
    when it runs. `have` is the probe's verdict, by default whether every tool
    is on PATH; `why` the reason, by default "needs" and the tools. Every skip
    for a missing tool goes through here. Under FORBID_SKIP a skip whose tools
    are all on PATH is an AssertionError instead."""
    tools = [tools] if isinstance(tools, str) else list(tools)
    on_path = all(WHICH(tool) for tool in tools)
    if have is None:
        have = on_path
    if have:
        return None
    why = why or "needs %s" % " and ".join(tools)
    if FORBID_SKIP and on_path:
        raise AssertionError(
            "%s, yet %s on PATH and YOLOBOX_FORBID_SKIP=1: the probe that judged "
            "it missing is wrong" % (why, " and ".join(tools))
        )
    return why


def needs_tool(tools, have=None, why=None):
    """unittest.skip for a test class or method that needs `tools`, judged by
    missing_tool_reason when the module loads."""
    reason = missing_tool_reason(tools, have, why)
    return unittest.skipIf(reason is not None, reason)


def require_tool(tools, have=None, why=None):
    """Skip the running test unless missing_tool_reason lets it run."""
    reason = missing_tool_reason(tools, have, why)
    if reason is not None:
        raise unittest.SkipTest(reason)


def render_fact(key, value):
    """One fact as the words of its line, as nix/lib/args.nix renders it: a
    string or an integer as itself, a list of strings as its words, a map of
    strings as key:value words in key order. Any other shape is refused."""
    if isinstance(value, bool):
        pass
    elif isinstance(value, (str, int)):
        return str(value)
    elif isinstance(value, list) and all(isinstance(w, str) for w in value):
        return " ".join(value)
    elif isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
        return " ".join("%s:%s" % (k, value[k]) for k in sorted(value))
    raise TypeError(
        "facts.json: %s is %s, which has no YOLOBOX_%s rendering"
        % (key, type(value).__name__, key.upper())
    )


def facts_sh(**overrides):
    """/etc/yolobox/facts.sh: YOLOBOX_<KEY>=value per fact, rendered by
    render_fact and shell-quoted. `overrides` replace or add facts."""
    facts = dict(FACTS, **overrides)
    return "".join(
        "YOLOBOX_%s=%s\n" % (key.upper(), shlex.quote(render_fact(key, value)))
        for key, value in facts.items()
    )


def is_gnu(tool: str) -> bool:
    """Whether `tool` on PATH is GNU's: `tool --version` says GNU. A missing
    tool, or one that rejects --version (BSD), is not."""
    try:
        probe = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, timeout=20
        )
    except OSError:
        return False
    return probe.returncode == 0 and "GNU" in probe.stdout


def bash_version() -> tuple:
    """The (major, minor) of the bash on PATH; (0, 0) when there is none. A
    bash that hangs, fails or prints anything but two numbers is an error
    naming what it did, never a version."""
    argv = ["bash", "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"']
    try:
        probe = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return (0, 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError("%r did not answer within 20 s" % argv)
    words = probe.stdout.split()
    if probe.returncode != 0 or len(words) != 2 or not all(w.isdigit() for w in words):
        raise RuntimeError(
            "%r exited %d and printed %r, not a major and a minor version"
            % (argv, probe.returncode, probe.stdout + probe.stderr)
        )
    return (int(words[0]), int(words[1]))


class ShellCase(unittest.TestCase):
    """A scratch directory `tmp` with `bin_dir` of fakes first on `env["PATH"]`."""

    timeout = 20

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.env = {
            # The ambient PATH stays after the fakes: the scripts need mktemp,
            # find and the rest, and on NixOS they are not in /usr/bin.
            "PATH": os.pathsep.join(
                [str(self.bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            # Without it bash expands ~ from the account database: the real home.
            "HOME": SCRATCH_HOME,
        }

    def fake(self, name, text):
        """An executable `name` in bin_dir with `text`, which starts with its
        own shebang line."""
        path = self.bin_dir / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def run_bash(self, script, *args, env=None, **kwargs):
        """Run `script` under bash with the three options, `env` over self.env."""
        return subprocess.run(
            [*BASH, str(script), *args],
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            timeout=self.timeout,
            **kwargs,
        )
