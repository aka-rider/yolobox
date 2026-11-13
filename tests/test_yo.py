"""`yo`'s functions, argv builders and path handling, through the package namespace.

Tools whose absence skips tests: git (the repo-toplevel tests), ssh (the ssh
option tests), and a Python with `os.pidfd_open` or `select.kqueue` (the
pid-exit watch).
"""

import argparse
import contextlib
import errno
import fcntl
import http.server
import importlib.util
import io
import itertools
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from shell_harness import is_gnu, needs_tool, require_tool

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "yolobox_cli"
# A release this yo can build.
A_RELEASE = "2.3.4"

# The host's CPU count in tests that create a box or read --cpus, so the default
# CPU count (min(4, host - 1)) and the --cpus range do not depend on the machine
# that runs them. FakeHome pins it; a test that needs another count patches
# os.cpu_count itself.
HOST_CPUS = 16

# The host account every loaded yo runs as (load_yo).
HOST_ACCOUNT = "op"


def host_cpus(count=HOST_CPUS):
    return mock.patch.object(os, "cpu_count", return_value=count)


REAL_CREATE_CONNECTION = socket.create_connection


def no_tcp_to_the_host(address, *args, **kwargs):
    raise ConnectionRefusedError(
        errno.ECONNREFUSED, "no test may reach a real TCP port: %s:%s" % address[:2]
    )


def refuse_tcp_to_the_host():
    """Every module that loads yo calls this from its setUpModule, and the
    refusal ends with the module.

    yo's only TCP client is unlock.ssh_banner, and Lima's default port is the
    one a real box on the machine running the tests listens on. A test that
    does not fake the banner would meet that box's stage-2 sshd and take a
    different branch than on a machine with none. Refused is what the tests
    were written against; a test that serves its own port restores
    REAL_CREATE_CONNECTION."""
    refusal = mock.patch.object(socket, "create_connection", no_tcp_to_the_host)
    refusal.start()
    unittest.addModuleCleanup(refusal.stop)


def setUpModule():
    refuse_tcp_to_the_host()


class YoNamespace:
    """The yolobox_cli package as one namespace, read live.

    `yo.NAME` finds NAME in whichever module defines it, at access time, and
    `yo.proc` is the module itself. A name two modules define as different
    objects is refused rather than guessed, so a rename that collides fails here
    at the first read. Writes are refused too: a patch has to name the module
    whose global the code reads, `mock.patch.object(yo.proc, "run")`.
    """

    def __init__(self, modules):
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "REPO", REPO)
        object.__setattr__(self, "PKG_DIR", PKG)

    def __getattr__(self, name):
        modules = object.__getattribute__(self, "_modules")
        if name in modules:
            return modules[name]
        if name.startswith("__"):
            raise AttributeError(name)
        found = {
            module_name: vars(module)[name]
            for module_name, module in modules.items()
            if name in vars(module)
        }
        if not found:
            raise AttributeError("no module of yolobox_cli defines %r" % name)
        values = list(found.values())
        if any(value is not values[0] for value in values[1:]):
            raise AttributeError(
                "%r is defined by %s as different objects; name the module"
                % (name, ", ".join(sorted(found)))
            )
        return values[0]

    def __setattr__(self, name, value):
        raise AttributeError(
            "cannot set %r on the namespace; patch yo.<module>.%s" % (name, name)
        )


def load_yo(name="yo"):
    """Execute the yolobox_cli package as `name`, a copy with its own globals.

    A test that needs another machine loads a copy and calls its
    `state.set_box`, so it never disturbs the shared `yo`. Remove a copy with
    `unload_yo(name)`.

    The copy drives the Lima driver on the default machine, whatever host runs
    the tests, so `main()` and every function that reads the driver behave as
    they do on a Mac. Its host account is HOST_ACCOUNT, whoever runs the tests.
    """
    spec = importlib.util.spec_from_file_location(
        name, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    try:
        spec.loader.exec_module(package)
        modules = {
            path.stem: importlib.import_module("%s.%s" % (name, path.stem))
            for path in sorted(PKG.glob("*.py"))
            if path.stem != "__init__"
        }
    except BaseException:
        unload_yo(name)
        raise
    modules["config"].HOST_PLATFORM = "darwin"
    # The operator is a fixed account, not whoever runs the tests: inside a box
    # that is the agent, the one name yo refuses for the operator.
    modules["release"].host_user = lambda: HOST_ACCOUNT
    modules["state"].set_box(modules["lima"].LIMA, modules["config"].DEFAULT_INSTANCE)
    return YoNamespace(modules)


def unload_yo(name):
    """Forget the package `name` and every module under it."""
    for module_name in [
        key for key in sys.modules if key == name or key.startswith(name + ".")
    ]:
        del sys.modules[module_name]


yo = load_yo()


def fake_bin(test, tools):
    """A scratch directory, removed after `test`, holding each of `tools`
    ({name: text, the text starting with its shebang}) as an executable.
    Returns it with `path`, the PATH that finds the fakes first."""
    bin_dir = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, bin_dir)
    for name, text in tools.items():
        tool = Path(bin_dir, name)
        tool.write_text(text)
        tool.chmod(0o755)
    return bin_dir, bin_dir + os.pathsep + os.environ["PATH"]


class VirtualClock:
    """time.sleep and time.monotonic for a wait under test, patched while it
    is entered: a sleep advances the clock the deadlines read, at once. A wait
    that sleeps more than MAX_SLEEPS times is polling forever."""

    MAX_SLEEPS = 2000

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.MAX_SLEEPS:
            raise AssertionError("yo is polling forever")
        self.now += seconds

    def __enter__(self):
        self.patched = contextlib.ExitStack()
        self.patched.enter_context(mock.patch.object(yo.time, "sleep", self.sleep))
        self.patched.enter_context(
            mock.patch.object(yo.time, "monotonic", lambda: self.now)
        )
        return self

    def __exit__(self, *exc):
        self.patched.close()
        return False


# What ssh says when nothing listens on the box's port.
NOT_LISTENING = (255, "ssh: connect to host 127.0.0.1 port 60022: Connection refused")


class FakeHelper:
    """The initrd's `yolobox-unlock` ssh session: scripted stdout lines, exit
    code and stderr; `stdin.sent` is what yo wrote to it."""

    class Stdin:
        def __init__(self):
            self.sent = b""

        def write(self, data):
            self.sent += data

        def close(self):
            pass

    def __init__(self, lines, code, stderr):
        self.stdout = io.BytesIO("".join(line + "\n" for line in lines).encode())
        self.stderr = io.BytesIO(stderr.encode())
        self.stdin = self.Stdin()
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self):
        return self.code


def box_lock_is_held(state=None):
    """Whether some yo holds the box lock, probed as another yo would: through
    a descriptor of its own, never waiting. `state` is the namespace's state
    module, yo's by default."""
    path = (state or yo.state).box_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # drops the lock when this probe got it
    return False


@contextlib.contextmanager
def box_lock_held_elsewhere(state=None):
    """The box lock held by another yo, through a descriptor of its own."""
    path = (state or yo.state).box_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def guest_words(remote):
    """The words of the command an ssh argv's last element runs in the guest,
    without the C locale yo runs every guest command in."""
    words = shlex.split(remote)
    return words[2:] if words[:2] == ["env", "LC_ALL=C"] else words


AGENT_LOGIN = ["echo", yo.sshx.AGENT_LOGIN_WORD]


def ssh_ends(words, ssh_log, code, stdout="", stderr=""):
    """(stdout, stderr) of an ssh that ran `words` in the guest and ended with
    `code`: the agent's login prints its word when it ran, and a 255's stderr
    is ssh's own, which an ssh given -E (`ssh_log`) writes to that file."""
    if code == 0 and list(words) == AGENT_LOGIN:
        stdout = yo.sshx.AGENT_LOGIN_WORD + "\n"
    if code == yo.SSH_UNREACHABLE and ssh_log is not None and stderr:
        with open(ssh_log, "a") as log:
            log.write(stderr)
        stderr = ""
    return stdout, stderr


def ssh_log_of(argv):
    """The -E file an ssh argv names, or None."""
    return argv[argv.index("-E") + 1] if "-E" in argv else None


def proved_initrd(facts, namespace=None):
    """An InitrdEndpoint of `facts`, made the one way there is: by
    initrd_endpoint() under the box lock, against a listener and a banner
    faked as the initrd's."""
    ns = namespace or yo
    driver = ns.state.box.driver
    banner = "SSH-2.0-OpenSSH_10.0 " + ns.config.INITRD_SSH_BANNER_MARK
    with (
        mock.patch.object(driver, "ssh_facts", return_value=facts),
        mock.patch.object(driver, "verify_vm_listener"),
        mock.patch.object(ns.unlock, "ssh_banner", return_value=banner),
        ns.state.box_lock(wait=True) as held,
    ):
        gate = ns.unlock.initrd_endpoint(held)
    assert isinstance(gate, ns.unlock.InitrdEndpoint), gate
    return gate


def locked(namespace, step, *args, **kwargs):
    """`step(held, *args, **kwargs)` under the box lock of `namespace`, a loaded
    yo, as the command that runs it holds the lock."""
    with namespace.state.box_lock(wait=True) as held:
        return step(held, *args, **kwargs)


# The pid of the Lima hostagent the fakes run, which holds the box's ssh port.
HOSTAGENT_PID = 4242


@contextlib.contextmanager
def hostagent_holds_the_port(namespace=None):
    """The Lima instance's hostagent, and it alone, listening on the box's
    ssh port, for a test whose subject is not the listener check."""
    lima = (namespace or yo).lima
    with (
        mock.patch.object(lima.LIMA, "keepalive_pid", return_value=HOSTAGENT_PID),
        mock.patch.object(lima, "tcp_listener_pids", return_value=[HOSTAGENT_PID]),
    ):
        yield


def with_home(path):
    return mock.patch.dict(os.environ, {"HOME": path}, clear=False)


class FakeHome(unittest.TestCase):
    def setUp(self):
        # Under /tmp, not the default temp dir: macOS's is /var/folders/xx/.../T
        # (49 bytes), and $HOME/.lima/yolobox/ssh-agent.sock plus ssh's 17-byte
        # temporary suffix then passes the 104-byte unix socket limit, so every
        # test that builds an ssh argv would fail. /tmp is a symlink on macOS,
        # and left unresolved here, the way a user's $HOME can be.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        patcher = with_home(self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        cpus = host_cpus()
        cpus.start()
        self.addCleanup(cpus.stop)


class TestShown(unittest.TestCase):
    """ui.shown renders a guest-controlled name inside a message: readable when
    it is plain, escaped when it holds anything a terminal or a reader could
    mistake for something else."""

    def test_a_plain_name_is_unchanged(self):
        for name in ("proj/node_modules", "café ünï", 'it\'s "quoted"', "a\\b", ""):
            with self.subTest(name=name):
                self.assertEqual(yo.ui.shown(name), name)

    def test_unprintable_characters_are_spelled_out(self):
        cases = {
            "a\x1b[2Jb": "a\\x1b[2Jb",
            "a\nFAIL: forged": "a\\nFAIL: forged",
            "a\rb": "a\\rb",
            "a\tb": "a\\tb",
            "a\x00b": "a\\x00b",
            "a\x9bb": "a\\x9bb",
            "a\u202eb": "a\\u202eb",
            "a\udcffb": "a\\udcffb",
            "\x1b\\n": "\\x1b\\\\n",
        }
        for name, shown in cases.items():
            with self.subTest(name=name):
                self.assertEqual(yo.ui.shown(name), shown)

    def test_a_shown_name_is_one_printable_line(self):
        for name in ("\x1b]52;c;ZXZpbA==\x07", "a\nb\rc", "\x85\u2028"):
            with self.subTest(name=name):
                self.assertTrue(yo.ui.shown(name).isprintable())


class TestRunToolMissing(unittest.TestCase):
    def test_a_missing_executable_is_a_yo_error_naming_it(self):
        with mock.patch.object(yo.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(yo.YoError) as caught:
                yo.run(["scp", "a", "b"])
        self.assertEqual(caught.exception.message, "scp not found on PATH")

    def test_spawn_of_a_missing_executable_is_a_yo_error_naming_it(self):
        with mock.patch.object(yo.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(yo.YoError) as caught:
                yo.spawn(["virsh", "list"], text=True)
        self.assertEqual(caught.exception.message, "virsh not found on PATH")

    def test_exec_of_a_missing_executable_is_a_yo_error_naming_it(self):
        with (
            mock.patch.object(yo.signal, "signal"),
            mock.patch.object(yo.os, "execvpe", side_effect=FileNotFoundError),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.exec_process(["ssh", "host"])
        self.assertEqual(caught.exception.message, "ssh not found on PATH")


class TestToolsSpeakTheCLocale(unittest.TestCase):
    """yo matches the words tools print, which a translated tool changes, so
    every way yo runs a tool runs it under LC_ALL=C, whatever the operator's
    locale, and keeps the rest of the environment."""

    PRINT_LOCALE = ["sh", "-c", 'echo "$LC_ALL $LANG"']

    def setUp(self):
        german = {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
        patcher = mock.patch.dict(os.environ, german)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_host_tool_a_guest_session_and_a_spawned_tool_run_in_c(self):
        runs = {
            "host": yo.run(self.PRINT_LOCALE).stdout,
            "guest": yo.run(self.PRINT_LOCALE, guest="agent").stdout,
            "given env": yo.run(self.PRINT_LOCALE, env={"LANG": "x"}).stdout,
        }
        child = yo.spawn(self.PRINT_LOCALE, stdout=subprocess.PIPE, text=True)
        runs["spawned"] = child.communicate()[0]
        self.assertEqual(
            runs,
            {
                "host": "C de_DE.UTF-8\n",
                "guest": "C de_DE.UTF-8\n",
                "given env": "C x\n",
                "spawned": "C de_DE.UTF-8\n",
            },
        )

    def test_a_streamed_guest_session_runs_in_c(self):
        printed = []
        with mock.patch.object(yo.ui, "out", printed.append):
            yo.run(self.PRINT_LOCALE, filtered=True)
        self.assertEqual(printed, [yo.ui.quoted_line("C de_DE.UTF-8")])


class TestRunInADirectory(FakeHome):
    """Every way yo runs a tool honours `cwd`, so a tool that writes beside
    itself (limactl's rejected-edit file) writes where yo says."""

    WAYS = {
        "host": {},
        "guest": {"guest": "agent"},
        "streamed": {"filtered": True},
        "to the terminal": {"capture": False},
    }

    def test_a_directory_that_is_missing_is_said_so_and_a_missing_tool_still_is(self):
        gone = os.path.join(self.home, "gone")
        for name, way in self.WAYS.items():
            with self.subTest(way=name):
                with self.assertRaises(yo.YoError) as caught:
                    yo.run(["touch", "witness"], cwd=gone, **way)
                self.assertEqual(
                    caught.exception.message,
                    "cannot run touch in %s: the directory does not exist" % gone,
                )
                with self.assertRaises(yo.YoError) as caught:
                    yo.run(["no-such-tool-here"], cwd=self.home, **way)
                self.assertEqual(
                    caught.exception.message, "no-such-tool-here not found on PATH"
                )

    def test_each_way_of_running_a_tool_runs_it_in_the_directory_asked_for(self):
        for name, way in self.WAYS.items():
            with self.subTest(way=name):
                here = Path(self.home) / name.replace(" ", "-")
                here.mkdir()
                with mock.patch.object(yo.ui, "out"):
                    yo.run(["touch", "witness"], cwd=str(here), check=True, **way)
                self.assertEqual(os.listdir(here), ["witness"])

    def test_no_directory_runs_the_tool_in_yos_own(self):
        here = os.getcwd()
        self.assertEqual(
            yo.run(["pwd", "-P"], check=True).stdout.strip(), os.path.realpath(here)
        )


class TestAtomicWrite(FakeHome):
    def test_an_interrupt_before_the_replace_removes_the_temp_file(self):
        target = os.path.join(self.home, "file")
        with mock.patch.object(yo.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                yo.proc.atomic_write(target, "text", 0o600)
        self.assertEqual(os.listdir(self.home), [])

    def test_the_file_appears_whole_with_the_mode_asked_for(self):
        target = os.path.join(self.home, "file")
        yo.proc.atomic_write(target, "text", 0o640)
        self.assertEqual(Path(target).read_text(), "text")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)


class TestYoNamespace(unittest.TestCase):
    """The tests' view of the package: one name, one object, read-only."""

    SHARED = object()

    def namespace(self):
        return YoNamespace(
            {
                "proc": types.SimpleNamespace(run="proc.run", SHARED=self.SHARED),
                "sshx": types.SimpleNamespace(
                    run="sshx.run", SHARED=self.SHARED, only="here"
                ),
            }
        )

    def test_a_name_is_found_in_the_module_that_defines_it(self):
        self.assertEqual(self.namespace().only, "here")

    def test_one_object_imported_into_two_modules_is_that_object(self):
        self.assertIs(self.namespace().SHARED, self.SHARED)

    def test_a_module_name_is_the_module(self):
        self.assertEqual(self.namespace().proc.run, "proc.run")

    def test_a_name_two_modules_define_differently_is_refused_naming_both(self):
        with self.assertRaisesRegex(AttributeError, "'run' is defined by proc, sshx"):
            self.namespace().run

    def test_an_unknown_name_is_refused(self):
        with self.assertRaisesRegex(AttributeError, "no module of yolobox_cli"):
            self.namespace().lizard

    def test_a_write_is_refused_and_names_the_module_to_patch(self):
        with self.assertRaisesRegex(AttributeError, "patch yo.<module>.only"):
            self.namespace().only = "elsewhere"


class TestFileChokepointsFailAsYoErrors(unittest.TestCase):
    """A host file yo cannot make or delete is one message, never a traceback."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.file = Path(scratch.name, "file")
        self.file.write_text("")

    def test_a_private_dir_under_a_file_names_the_path(self):
        wanted = str(self.file / "dir")
        with self.assertRaisesRegex(
            yo.YoError, "cannot make the directory %s" % wanted
        ):
            yo.proc.private_dir(wanted)

    def test_a_file_already_gone_is_removed(self):
        yo.proc.remove_file(str(self.file.parent / "never-there"))

    def test_a_directory_is_not_a_file_to_remove(self):
        with self.assertRaisesRegex(yo.YoError, "cannot delete %s" % self.file.parent):
            yo.proc.remove_file(str(self.file.parent))


class TestChecked(unittest.TestCase):
    """proc.checked: the one message of a command that failed."""

    def failed(self, stdout, stderr):
        return yo.proc.GuestRun(["ssh"], 3, stdout, stderr)

    def test_a_success_comes_back_as_it_is(self):
        completed = subprocess.CompletedProcess(["ssh"], 0, "out", "")
        self.assertIs(yo.proc.checked(completed, "x"), completed)

    def test_a_failure_with_only_binary_stdout_is_a_yo_error_with_that_text(self):
        # agent_paths runs the helper with binary=True: stdout is bytes there.
        with self.assertRaises(yo.YoError) as caught:
            yo.proc.checked(self.failed(b"find: odd\n", ""), "projects")
        self.assertEqual(
            caught.exception.message, "projects failed (exit 3):\n  | find: odd"
        )

    def test_a_failure_that_said_nothing_is_a_yo_error_that_says_so(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.proc.checked(self.failed(b"", ""), "projects", "on the box")
        self.assertEqual(
            caught.exception.message,
            "projects failed on the box (exit 3) with no output",
        )

    def test_a_failure_whose_output_went_to_the_terminal_ends_at_the_exit(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.proc.checked(subprocess.CompletedProcess(["x"], 3, None, None), "x")
        self.assertEqual(caught.exception.message, "x failed (exit 3)")

    def test_stderr_comes_before_stdout(self):
        with self.assertRaisesRegex(yo.YoError, "exit 3.:\n  \\| boom"):
            yo.proc.checked(self.failed("out", "boom\n"), "x")

    def test_a_host_tools_words_stay_inline_and_unquoted(self):
        host_tool = subprocess.CompletedProcess(["virsh"], 3, "", "error: lizard\n")
        with self.assertRaises(yo.YoError) as caught:
            yo.proc.checked(host_tool, "virsh x")
        self.assertEqual(
            caught.exception.message, "virsh x failed (exit 3): error: lizard"
        )


class TestMirror(FakeHome):
    def test_home_and_subdirectory(self):
        self.assertEqual(yo.mirror(self.home), yo.AGENT_HOME)
        self.assertEqual(yo.mirror(self.home + "/a/b"), yo.AGENT_HOME + "/a/b")

    def test_home_with_a_trailing_slash_still_mirrors(self):
        with with_home(self.home + "/"):
            self.assertEqual(yo.mirror(self.home + "/a"), yo.AGENT_HOME + "/a")
            self.assertEqual(yo.mirror(self.home), yo.AGENT_HOME)

    def test_outside_home(self):
        self.assertIsNone(yo.mirror("/etc/passwd"))
        self.assertIsNone(yo.mirror(self.home + "x/y"))

    def test_symlinked_spelling_is_not_resolved(self):
        target = os.path.join(self.home, "src")
        os.mkdir(target)
        link = os.path.join(self.home, "code")
        os.symlink(target, link)
        self.assertEqual(yo.mirror(link + "/proj"), yo.AGENT_HOME + "/code/proj")


class TestMirrorWithASymlinkedHome(FakeHome):
    """$HOME spelled through a symlink (macOS's /var -> /private/var, or a home on
    another volume): a cwd the shell did not report comes back from getcwd() resolved."""

    def setUp(self):
        super().setUp()
        self.real = os.path.join(self.home, "volume", "alice")
        os.makedirs(os.path.join(self.real, "proj"))
        self.real = os.path.realpath(self.real)
        self.link = os.path.join(self.home, "alice")
        os.symlink(self.real, self.link)
        patcher = with_home(self.link)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_path_in_either_spelling_of_home_mirrors_to_the_same_place(self):
        for spelling in (self.link, self.real):
            with self.subTest(spelling=spelling):
                self.assertEqual(yo.mirror(spelling), yo.AGENT_HOME)
                self.assertEqual(yo.mirror(spelling + "/proj"), yo.AGENT_HOME + "/proj")

    def test_a_path_that_only_looks_like_the_resolved_home_is_outside(self):
        self.assertIsNone(yo.mirror(self.real + "x/proj"))
        self.assertIsNone(yo.mirror(os.path.dirname(self.real)))

    def test_a_cwd_without_a_usable_pwd_still_mirrors(self):
        previous = os.getcwd()
        os.chdir(os.path.join(self.link, "proj"))
        self.addCleanup(os.chdir, previous)
        with mock.patch.dict(os.environ):
            os.environ.pop("PWD", None)
            self.assertEqual(yo.mirror(yo.logical_cwd()), yo.AGENT_HOME + "/proj")


class TestGuestPath(unittest.TestCase):
    def test_accepts_home_and_children(self):
        self.assertEqual(yo.guest_path(yo.AGENT_HOME), yo.AGENT_HOME)
        self.assertEqual(yo.guest_path(yo.AGENT_HOME + "/x"), yo.AGENT_HOME + "/x")

    def test_rejects_sibling_prefix(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/home/agentx": no yo'
        ):
            yo.guest_path("/home/agentx")

    def test_rejects_traversal(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/home/agent/\\.\\./x": no yo'
        ):
            yo.guest_path(yo.AGENT_HOME + "/../x")

    def test_rejects_newline(self):
        with self.assertRaisesRegex(
            yo.YoError, "refusing a guest path with a newline or NUL byte"
        ):
            yo.guest_path(yo.AGENT_HOME + "/a\nb")

    def test_default_roots_refuse_tmp(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/tmp/x": no yo command may'
        ):
            yo.guest_path("/tmp/x")


LIMA_FACTS = yo.SshFacts(60022, "user", "/Users/user/.lima/_config/user", "none")


class TestSshRunArgv(FakeHome):
    def capture_argv(self, role=None, **kwargs):
        seen = {}

        def fake_run(argv, **rest):
            seen["argv"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.proc, "run", fake_run):
            yo.ssh_run(role or yo.OPERATOR, ["true"], **kwargs)
        return seen["argv"]

    def test_a_guest_command_runs_in_the_c_locale_whatever_the_guests_is(self):
        # yo reads lsblk's, df's and ssh-add's words. The remote command runs
        # under the guest's shell, here a local sh in another locale.
        seen = {}

        def fake_run(argv, **rest):
            seen["remote"] = argv[-1]
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.proc, "run", fake_run):
            yo.ssh_run(yo.AGENT, ["sh", "-c", 'printf "%s" "${LC_ALL-unset}"'])
        ran = subprocess.run(
            ["sh", "-c", seen["remote"]],
            env=dict(os.environ, LC_ALL="fr_FR.UTF-8"),
            capture_output=True,
            text=True,
        )
        self.assertEqual((ran.returncode, ran.stdout), (0, "C"))

    def test_no_stdin_closes_it_with_dash_n(self):
        self.assertIn("-n", self.capture_argv())

    def test_piped_stdin_keeps_it_open(self):
        self.assertNotIn("-n", self.capture_argv(stdin=b"script"))

    def test_agent_role_carries_user_and_control_path(self):
        argv = yo.ssh_options(yo.AGENT, mux=True)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=%s/.lima/yolobox/ssh-agent.sock" % self.home, argv)


class TestAMachineNameMustLeaveRoomForItsSockets(unittest.TestCase):
    """The longest socket under a box's instance directory is the agent's ssh
    master, which ssh binds at its ControlPath plus 17 bytes; a unix socket
    path must be shorter than 104 bytes on macOS and 108 on Linux. -m is
    refused at parse time, before anything runs, when $HOME plus the name
    leaves no room."""

    HOSTS = (("darwin", ".lima", 104), ("linux", ".local/state/yolobox/instances", 108))

    def parse(self, home, platform, *argv):
        """(exit code, stderr) of `yo ARGV` under `home` on `platform`; exit
        None when it parsed."""
        stderr = io.StringIO()
        with (
            with_home(home),
            mock.patch.object(yo.config, "HOST_PLATFORM", platform),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                yo.build_parser().parse_args(list(argv))
            except SystemExit as refused:
                return refused.code, stderr.getvalue()
        return None, stderr.getvalue()

    def test_the_last_name_that_fits_is_accepted_and_one_byte_more_is_refused(self):
        # A home that leaves fewer bytes than the 55 a hostname allows.
        home = "/" + "h" * 20
        for platform, instances, limit in self.HOSTS:
            # <home>/<instances>/<name>/ssh-agent.sock, plus ssh's 17 bytes.
            room = limit - 1 - len("%s/%s//ssh-agent.sock" % (home, instances)) - 17
            with self.subTest(platform=platform):
                fits = self.parse(home, platform, "-m", "x" * room, "status")
                self.assertEqual(fits, (None, ""))
                name = "x" * (room + 1)
                code, said = self.parse(home, platform, "-m", name, "up")
                self.assertEqual(code, 2)
                self.assertIn(
                    "%s/%s/%s/ssh-agent.sock plus the 17 bytes of ssh's temporary "
                    "socket name is %d bytes, and a unix socket path must be shorter "
                    "than %d on this host: $HOME (%s) plus the machine name '%s' is "
                    "too long for yo's sockets; pick a shorter -m name"
                    % (home, instances, name, limit, limit, home, name),
                    " ".join(said.split()),
                )

    def test_a_home_too_long_for_any_name_refuses_the_default_machine_too(self):
        for platform, _, limit in self.HOSTS:
            with self.subTest(platform=platform):
                code, said = self.parse("/" + "d" * limit, platform, "status")
                self.assertEqual(code, 2)
                self.assertIn("unix socket path must be shorter", said)


class TestResolveCpOperands(unittest.TestCase):
    MIRROR = "/home/agent/src/proj"

    def test_requires_a_source_and_a_destination(self):
        with self.assertRaisesRegex(
            yo.YoError, "yo cp needs at least one source and a"
        ):
            yo.resolve_cp_operands([":a"], self.MIRROR)

    def test_all_local_is_refused_and_names_cp(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.resolve_cp_operands(["a.txt", "b.txt"], self.MIRROR)
        self.assertIn("cp", caught.exception.message)

    def test_mixed_side_sources_are_refused(self):
        with self.assertRaisesRegex(
            yo.YoError, "sources are split between the host and the"
        ):
            yo.resolve_cp_operands(["a.txt", ":b.txt", ":dest"], self.MIRROR)

    def test_guest_to_guest_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.resolve_cp_operands([":a", ":b"], self.MIRROR)
        self.assertIn("yo enter", caught.exception.message)

    def test_bare_colon_destination_resolves_to_the_mirror(self):
        plan = yo.resolve_cp_operands(["a.txt", ":"], self.MIRROR)
        self.assertEqual(plan.direction, "to_guest")
        self.assertEqual(plan.local_paths, ["a.txt"])
        self.assertEqual(plan.guest_paths, [self.MIRROR])

    def test_relative_guest_spelling_extends_the_mirror(self):
        plan = yo.resolve_cp_operands(["a.txt", ":sub/dir"], self.MIRROR)
        self.assertEqual(plan.guest_paths, [self.MIRROR + "/sub/dir"])

    def test_tilde_slash_is_under_agent_home(self):
        plan = yo.resolve_cp_operands(["a.txt", ":~/x"], self.MIRROR)
        self.assertEqual(plan.guest_paths, [yo.AGENT_HOME + "/x"])

    def test_bare_tilde_is_agent_home(self):
        plan = yo.resolve_cp_operands(["a.txt", ":~"], self.MIRROR)
        self.assertEqual(plan.guest_paths, [yo.AGENT_HOME])

    def test_absolute_guest_path_under_home_is_accepted(self):
        plan = yo.resolve_cp_operands(["a.txt", ":/home/agent/x"], self.MIRROR)
        self.assertEqual(plan.guest_paths, ["/home/agent/x"])

    def test_absolute_guest_path_outside_home_is_refused(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/etc/passwd": outside the'
        ):
            yo.resolve_cp_operands(["a.txt", ":/etc/passwd"], self.MIRROR)

    def test_absolute_guest_source_under_tmp_is_accepted(self):
        plan = yo.resolve_cp_operands([":/tmp/claude-1000/x", "dest"], self.MIRROR)
        self.assertEqual(plan.direction, "from_guest")
        self.assertEqual(plan.guest_paths, ["/tmp/claude-1000/x"])

    def test_tmp_itself_is_accepted(self):
        plan = yo.resolve_cp_operands(["a.txt", ":/tmp"], self.MIRROR)
        self.assertEqual(plan.guest_paths, ["/tmp"])

    def test_sibling_prefix_of_tmp_is_refused(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/tmpx/y": outside the guest'
        ):
            yo.resolve_cp_operands([":/tmpx/y", "dest"], self.MIRROR)

    def test_tmp_traversal_out_of_tmp_is_refused(self):
        with self.assertRaisesRegex(
            yo.YoError, 'refusing to target "/tmp/\\.\\./etc": outside the'
        ):
            yo.resolve_cp_operands([":/tmp/../etc", "dest"], self.MIRROR)

    def test_relative_spelling_cannot_reach_tmp(self):
        with self.assertRaisesRegex(yo.YoError, "refusing to target"):
            yo.resolve_cp_operands([":../../../tmp", "dest"], self.MIRROR)

    def test_guest_traversal_outside_home_is_refused(self):
        with self.assertRaisesRegex(yo.YoError, "refusing to target"):
            yo.resolve_cp_operands(["a.txt", ":../../../etc"], self.MIRROR)

    def test_relative_spelling_with_no_mirror_is_refused_and_names_tilde(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.resolve_cp_operands(["a.txt", ":"], None)
        self.assertIn("~", caught.exception.message)

    def test_multiple_local_sources_preserve_order(self):
        plan = yo.resolve_cp_operands(["a.txt", "b.txt", ":dump"], self.MIRROR)
        self.assertEqual(plan.local_paths, ["a.txt", "b.txt"])

    def test_multiple_guest_sources_preserve_order_and_direction(self):
        plan = yo.resolve_cp_operands([":a", ":b", "dest"], self.MIRROR)
        self.assertEqual(plan.direction, "from_guest")
        self.assertEqual(plan.guest_paths, [self.MIRROR + "/a", self.MIRROR + "/b"])
        self.assertEqual(plan.local_paths, ["dest"])

    def test_a_guest_source_named_twice_is_taken_once(self):
        plan = yo.resolve_cp_operands([":a", ":b", ":a/", ":./a", "dest"], self.MIRROR)
        self.assertEqual(plan.guest_paths, [self.MIRROR + "/a", self.MIRROR + "/b"])

    def test_guest_sources_that_would_land_under_one_name_are_refused(self):
        # They would merge in the staging directory, and APFS folds case.
        for operands in ((":x/log", ":y/Log"), (":~", ":/tmp/agent"), (":a/x", ":x")):
            with self.subTest(operands=operands):
                with self.assertRaisesRegex(yo.YoError, "would land under one name"):
                    yo.resolve_cp_operands(list(operands) + ["dest"], self.MIRROR)


class TestCpMkdirTarget(unittest.TestCase):
    def test_colon_dest_uses_resolved_dest_itself(self):
        self.assertEqual(
            yo.cp_mkdir_target(":", "/home/agent/src/proj", 1),
            "/home/agent/src/proj",
        )

    def test_trailing_slash_uses_resolved_dest_itself(self):
        self.assertEqual(
            yo.cp_mkdir_target(":dump/", "/home/agent/dump", 1), "/home/agent/dump"
        )

    def test_multiple_sources_use_resolved_dest_itself(self):
        self.assertEqual(
            yo.cp_mkdir_target(":dump", "/home/agent/dump", 2), "/home/agent/dump"
        )

    def test_single_plain_dest_uses_its_dirname(self):
        self.assertEqual(
            yo.cp_mkdir_target(":file.txt", "/home/agent/src/proj/file.txt", 1),
            "/home/agent/src/proj",
        )

    def test_a_dest_whose_last_component_is_a_dot_names_the_directory_itself(self):
        # ":." and ":./" mean ":"; "x/." and "x/.." mean the directory, as for cp.
        for spelling in (":.", ":./", ":x/.", ":x/..", ":..", ":~/.", ":/tmp/."):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    yo.cp_mkdir_target(spelling, "/home/agent/d", 1), "/home/agent/d"
                )


class CpHarness(FakeHome):
    def setUp(self):
        super().setUp()
        previous = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, previous)
        self.cwd = os.getcwd()
        for name in ("a.txt", "a:b.png", "-oProxyCommand=x"):
            Path(name).write_text("data")

    def run_cp(self, paths):
        calls = {"run": None, "ssh_run": []}

        def fake_run(argv, **kwargs):
            calls["run"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            calls["ssh_run"].append(list(argv))
            stdout, _ = ssh_ends(argv, None, 0, stdout=b"")
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
            # What cp says of each guest path goes to the test, not the run's stderr.
            mock.patch.object(yo.ui, "err"),
        ):
            args = yo.build_parser().parse_args(["cp"] + paths)
            yo.cmd_cp(args)
        return calls


class TestCpArgv(CpHarness):
    def test_to_guest_argv_carries_scp_flags_and_no_forward_agent(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        self.assertIn("-r", argv)
        self.assertIn("-p", argv)
        self.assertIn("-F", argv)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertNotIn("ForwardAgent", " ".join(argv))

    def test_scp_is_told_to_speak_sftp_rather_than_relying_on_its_default(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        self.assertIn("-s", argv[: argv.index("--")])

    def test_local_operands_become_absolute_after_a_double_dash(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        separator = argv.index("--")
        self.assertEqual(
            argv[separator + 1 :],
            [os.path.join(self.cwd, "a.txt"), "lima-yolobox:/home/agent/x"],
        )

    def test_a_source_with_a_colon_is_not_read_as_a_host(self):
        argv = self.run_cp(["a:b.png", ":~/x"])["run"]
        self.assertIn(os.path.join(self.cwd, "a:b.png"), argv)
        self.assertNotIn("a:b.png", argv)

    def test_a_source_that_looks_like_an_option_is_not_read_as_one(self):
        argv = self.run_cp(["--", "-oProxyCommand=x", ":~/x"])["run"]
        self.assertNotIn("-oProxyCommand=x", argv)
        self.assertGreater(
            argv.index(os.path.join(self.cwd, "-oProxyCommand=x")), argv.index("--")
        )

    def test_guest_destination_is_not_escaped(self):
        # scp never globs a destination; escaping would create a file whose
        # name contains the backslashes.
        argv = self.run_cp(["a.txt", ":~/a[1].txt"])["run"]
        self.assertIn("lima-yolobox:/home/agent/a[1].txt", argv)

    def test_an_empty_operand_is_refused(self):
        with self.assertRaisesRegex(yo.YoError, "yo cp got an empty operand"):
            self.run_cp(["", ":~/x"])

    def test_to_guest_remote_operand_is_prefixed_with_vm_host(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        self.assertIn("lima-yolobox:/home/agent/x", argv)

    def test_mkdir_runs_before_scp_for_to_guest(self):
        events = []

        def fake_run(argv, **kwargs):
            events.append(("run", list(argv)))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            events.append(("ssh_run", list(argv)))
            stdout, _ = ssh_ends(argv, None, 0)
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
            mock.patch.object(yo.ui, "err") as err,
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", "a.txt", ":~/x"]))
        err.assert_called_once_with("yo: guest path :~/x -> /home/agent/x")

        steps = [
            "mkdir" if argv[:2] == ["mkdir", "-p"] else kind
            for kind, argv in events
            if argv != AGENT_LOGIN
        ]
        self.assertEqual(steps, ["mkdir", "run"])
        self.assertEqual(events[-1][1][0], "scp")


class CpFromGuestHarness(CpHarness):
    """The guest is a directory on this machine: the fake ssh runs the guest's
    tar command against it, the real host tar unpacks the stream, and the files
    land in the download directory as they would from a box. The guest's tar
    and find are GNU's, so a test that runs them skips where this machine's
    are another; one that fakes the stream runs anywhere."""

    GUEST_HOME = "/home/agent"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.guest_tools_are_gnu = is_gnu("tar") and is_gnu("find")

    def setUp(self):
        super().setUp()
        self.guest_root = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.guest_root, ignore_errors=True)
        self.guest_commands = []  # each command the fake ssh ran in the guest
        self.unpacked_into = []  # the directory each host tar unpacked into

    def guest_path(self, relative):
        return os.path.join(self.guest_root, self.GUEST_HOME[1:], relative)

    def write_guest(self, guest_files):
        # guest_files: {path relative to the guest home: content, or None for
        # an empty directory}
        for name, text in guest_files.items():
            path = self.guest_path(name)
            if text is None:
                os.makedirs(path)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_text(text)

    def guest_find(self, role, argv, **kwargs):
        # The build-output listing, run on the guest directory: its absolute
        # paths live under guest_root here, and come back as the guest's.
        if argv[0] != "find":
            stdout, _ = ssh_ends(argv, None, 0, stdout=b"")
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        found = subprocess.run(
            [self.guest_root + arg if arg.startswith("/") else arg for arg in argv],
            capture_output=True,
        )
        root = os.fsencode(self.guest_root)
        listing = b"\0".join(
            path[len(root) :] if path.startswith(root) else path
            for path in found.stdout.split(b"\0")
        )
        return yo.proc.GuestRun(argv, found.returncode, listing, found.stderr.decode())

    def guest_tar(self, argv, **kwargs):
        # ssh's last argument is the shell command; -C names a guest directory,
        # which lives under guest_root here.
        command = shlex.split(argv[-1])
        self.guest_commands.append(guest_words(argv[-1]))
        moved = [
            self.guest_root + arg if previous == "-C" else arg
            for previous, arg in zip([""] + command, command)
        ]
        return subprocess.Popen(moved, **kwargs)

    def spawn(self, argv, **kwargs):
        if argv[0] == "ssh":
            return self.guest_tar(argv, **kwargs)
        self.unpacked_into.append(argv[argv.index("-C") + 1])
        return subprocess.Popen(list(argv), **kwargs)

    def copy(self, paths, guest_files=None, spawn=None):
        if spawn is None:
            require_tool(
                ["tar", "find"],
                have=self.guest_tools_are_gnu,
                why="the guest's tar and find are GNU's",
            )
        self.write_guest(guest_files or {})

        def nothing_built(role, argv, **kwargs):
            # A faked stream comes with a guest that holds no build output.
            stdout, _ = ssh_ends(argv, None, 0, stdout=b"")
            return yo.proc.GuestRun(argv, 0, stdout, "")

        self.said = []
        with (
            mock.patch.object(yo.proc, "spawn", spawn or self.spawn),
            mock.patch.object(
                yo.sshx, "ssh_run", self.guest_find if spawn is None else nothing_built
            ),
            mock.patch.object(yo.ui, "err", side_effect=self.said.append),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp"] + paths))

    def filling_staging(self, fill, fails=False):
        """A download whose guest says nothing and whose host tar is `fill`,
        called with the staging directory, then exits (non-zero if `fails`)."""

        def spawn(argv, **kwargs):
            if argv[0] != "tar":
                return subprocess.Popen(["true"], **kwargs)
            fill(argv[argv.index("-C") + 1])
            return subprocess.Popen(["false" if fails else "true"], **kwargs)

        return spawn

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def leftovers(self, directory):
        return [n for n in os.listdir(directory) if n.startswith(".yo-cp-")]

    def unprefixed_lines(self, reported):
        """The lines of a report that do not start a `yo: ` message: a hostile
        name that forged one would show up here."""
        return [line for line in reported.splitlines() if not line.startswith("yo: ")]

    def pull(self, guest_files, source=":~/proj"):
        """A copy of `source` into dest/; what yo said on stderr."""
        self.copy([source, "dest/"], guest_files)
        return "".join(line + "\n" for line in self.said)

    def landed(self):
        found = []
        for directory, _, files in os.walk("dest"):
            found += [os.path.join(directory, f) for f in files]
        return sorted(found)


class TestTheHostTarIsToldToLandDataOnly(unittest.TestCase):
    """The agent writes the download's stream, so the host's tar, whichever
    flavour it is, keeps no owner, ACL, xattr or file flag from it."""

    def unpack_argv(self, says):
        _, path = fake_bin(self, {"tar": "#!/bin/sh\nprintf '%%s\\n' '%s'\n" % says})
        with mock.patch.dict(os.environ, {"PATH": path}):
            return yo.cp.host_tar_unpack_argv("/staging")

    def test_bsdtar_drops_owners_acls_xattrs_file_flags_and_mac_metadata(self):
        argv = self.unpack_argv("bsdtar 3.5.3 - libarchive 3.5.3 zlib/1.2.12")
        for flag in (
            "--no-same-owner",
            "--no-same-permissions",
            "--no-acls",
            "--no-xattrs",
            "--no-fflags",
            "--no-mac-metadata",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ["-C", "/staging"])

    def test_gnu_tar_drops_owners_acls_xattrs_and_selinux_labels(self):
        argv = self.unpack_argv("tar (GNU tar) 1.35")
        for flag in ("--no-same-owner", "--no-acls", "--no-xattrs", "--no-selinux"):
            self.assertIn(flag, argv)
        self.assertNotIn("--no-mac-metadata", argv)

    def test_a_tar_of_neither_flavour_is_refused_naming_what_it_said(self):
        for says in ("lizard", ""):
            with self.subTest(says=says):
                with self.assertRaisesRegex(yo.YoError, "neither bsdtar nor GNU tar"):
                    self.unpack_argv(says)


class TestCpFromGuestDownload(CpFromGuestHarness):
    """One tar stream from the guest, unpacked into staging on the host."""

    def test_a_download_lands_files_under_the_destination(self):
        os.makedirs("dest")
        self.pull({"proj/a": "1", "proj/sub/deeper/b": "2", "proj/empty": None})
        self.assertEqual(self.landed(), ["dest/proj/a", "dest/proj/sub/deeper/b"])
        self.assertTrue(os.path.isdir("dest/proj/empty"))
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_host_tar_that_cannot_start_ends_the_guest_side_too(self):
        os.makedirs("dest")
        started = []

        def spawn(argv, **kwargs):
            if argv[0] == "ssh":
                guest = subprocess.Popen(
                    [sys.executable, "-c", "import signal; signal.pause()"], **kwargs
                )
                started.append(guest)
                return guest
            raise yo.YoError("tar not found on PATH")

        with self.assertRaisesRegex(yo.YoError, "tar not found on PATH"):
            self.copy([":~/proj", "dest/"], spawn=spawn)
        (guest,) = started
        self.assertEqual(guest.returncode, -signal.SIGKILL)
        self.assertEqual(self.leftovers("dest"), [])

    def test_an_empty_directory_lands_as_one(self):
        self.copy([":~/empty", "here"], {"empty": None})
        self.assertEqual(os.listdir("here"), [])

    def test_names_with_spaces_dashes_newlines_and_wildcards_travel_intact(self):
        os.makedirs("dest")
        names = ["a b.txt", "-rf", "line\nbreak", "w[1]*?\\.txt", "sp ace/in"]
        self.pull({"proj/%s" % name: name for name in names})
        for name in names:
            self.assertEqual(self.read("dest/proj/%s" % name), name)

    def test_non_ascii_names_land_byte_for_byte_from_a_tar_in_the_c_locale(self):
        # The guest's GNU tar writes its default format, each name as its raw
        # bytes; the host's tar runs in yo's C locale and must land them as sent.
        names = ["café.txt", "日本語/ファイル.txt", "naïve/ü.md"]
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for directory in ("proj", "proj/日本語", "proj/naïve"):
                info = tarfile.TarInfo(directory)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            for name in names:
                info = tarfile.TarInfo("proj/" + name)
                info.size = len(name.encode())
                tar.addfile(info, io.BytesIO(name.encode()))
        archive = Path(self.guest_root, "stream.tar")
        archive.write_bytes(stream.getvalue())
        host_spawn = yo.proc.spawn

        def spawn(argv, **kwargs):
            if argv[0] == "ssh":
                return subprocess.Popen(["cat", str(archive)], **kwargs)
            return host_spawn(argv, **kwargs)

        os.makedirs("dest")
        self.copy([":~/proj", "dest/"], spawn=spawn)
        self.assertEqual(
            [os.fsencode(path) for path in self.landed()],
            sorted(os.fsencode("dest/proj/" + name) for name in names),
        )
        for name in names:
            self.assertEqual(self.read("dest/proj/" + name), name)

    def test_a_source_whose_name_starts_with_a_dash_is_not_an_option(self):
        self.copy([":~/-rf", "out"], {"-rf": "data"})
        self.assertEqual(self.read("out"), "data")

    def test_nothing_but_the_one_tar_runs_in_the_guest(self):
        os.makedirs("dest")
        self.pull({"proj/a": "1"})
        self.assertEqual([command[0] for command in self.guest_commands], ["tar"])

    def test_the_guest_tar_runs_in_the_c_locale(self):
        # yo names what the guest's tar printed, whatever the guest's locale:
        # its command, run by a local sh in another locale, sees C.
        def spawn(argv, **kwargs):
            remote = shlex.split(argv[-1])
            probe = remote[: -len(guest_words(argv[-1]))] + ["sh", "-c", "echo $LC_ALL"]
            ran = subprocess.run(
                probe,
                env=dict(os.environ, LC_ALL="fr_FR.UTF-8"),
                capture_output=True,
                text=True,
            )
            raise Reached(ran.stdout)

        with (
            mock.patch.object(yo.proc, "spawn", spawn),
            mock.patch.object(yo.cp, "guest_build_output", return_value=[]),
            mock.patch.object(yo.cp, "host_tar_unpack_argv", return_value=["tar"]),
        ):
            with self.assertRaises(Reached) as reached:
                yo.cp.fetch_guest_sources(["/home/agent/proj"], self.home)
        self.assertEqual(reached.exception.args, ("C\n",))

    def test_overlapping_sources_each_land_under_their_own_name(self):
        os.makedirs("dest")
        self.copy([":~/proj", ":~/proj/a", "dest/"], {"proj/a/b": "1"})
        self.assertEqual(self.read("dest/a/b"), "1")
        self.assertEqual(self.read("dest/proj/a/b"), "1")

    def test_an_absent_source_fails_with_what_the_guest_said(self):
        os.makedirs("dest")
        with self.assertRaises(yo.YoError) as caught:
            self.pull({"other/a": "1"})
        message = caught.exception.message
        self.assertIn("cannot read /home/agent/proj in the guest", message)
        self.assertIn(yo.ui.GUEST_LINE_PREFIX, message)
        self.assertIn("No such file or directory", message)
        self.assertEqual(os.listdir("dest"), [])

    def test_a_guest_that_says_nothing_and_fails_is_still_an_error(self):
        os.makedirs("dest")

        def spawn(argv, **kwargs):
            return subprocess.Popen(["false"], **kwargs)

        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/proj", "dest/"], spawn=spawn)
        self.assertIn("with no output", caught.exception.message)
        self.assertEqual(os.listdir("dest"), [])

    def test_a_stream_the_host_tar_cannot_unpack_is_an_error_naming_tar(self):
        os.makedirs("dest")

        def spawn(argv, **kwargs):
            if argv[0] == "ssh":
                return subprocess.Popen(["echo", "not a tar archive"], **kwargs)
            return subprocess.Popen(list(argv), **kwargs)

        with self.assertRaisesRegex(yo.YoError, "cannot unpack the download"):
            self.copy([":~/proj", "dest/"], spawn=spawn)
        self.assertEqual(os.listdir("dest"), [])

    def test_a_crafted_stream_cannot_write_outside_the_staging_directory(self):
        # The agent owns its PATH, so the stream can be anything: members that
        # climb out, absolute ones, and a file through a symlink member.
        os.makedirs("dest")
        Path("victim").write_text("precious")
        crafted = os.path.join(self.guest_root, "crafted.tar")
        with tarfile.open(crafted, "w") as archive:
            for name in ("../escaped", "/abs-escaped", "proj/ok"):
                member = tarfile.TarInfo(name)
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            link = tarfile.TarInfo("proj/lnk")
            link.type = tarfile.SYMTYPE
            link.linkname = os.path.join(self.cwd, "victim")
            archive.addfile(link)
            through = tarfile.TarInfo("proj/lnk/through")
            through.size = 1
            archive.addfile(through, io.BytesIO(b"x"))

        def spawn(argv, **kwargs):
            if argv[0] == "ssh":
                return subprocess.Popen(["cat", crafted], **kwargs)
            return subprocess.Popen(list(argv), **kwargs)

        with self.assertRaises(yo.YoError):
            self.copy([":~/proj", "dest/"], spawn=spawn)
        self.assertEqual(self.read("victim"), "precious")
        self.assertFalse(os.path.lexists("escaped"))
        self.assertFalse(os.path.lexists("dest/escaped"))
        self.assertFalse(os.path.lexists("/abs-escaped"))
        self.assertEqual(os.listdir("dest"), [])

    def test_a_guest_source_named_like_a_host_symlink_is_not_checked_here(self):
        # The upload check walks host paths. A guest operand only ever names a
        # guest file, so a host symlink with the same name is no reason to refuse.
        os.symlink("a.txt", "link")
        self.copy([":link", "out"], {"link": "guest data"})
        self.assertEqual(self.read("out"), "guest data")
        self.assertTrue(os.path.islink("link"))


class TestCpFromGuestLandsOnlyPlainEntries(CpFromGuestHarness):
    """The agent writes the stream, so only plain files and directories land:
    no link, special file or setuid bit reaches the Mac."""

    def pull_crafted(self, *members):
        """A pull of :~/proj whose guest sends a tar of (TarInfo, data) pairs."""
        os.makedirs("dest", exist_ok=True)
        crafted = os.path.join(self.guest_root, "crafted.tar")
        with tarfile.open(crafted, "w", format=tarfile.PAX_FORMAT) as archive:
            for member, data in members:
                member.size = len(data or b"")
                archive.addfile(member, io.BytesIO(data) if data else None)

        def spawn(argv, **kwargs):
            if argv[0] == "ssh":
                return subprocess.Popen(["cat", crafted], **kwargs)
            return subprocess.Popen(list(argv), **kwargs)

        self.copy([":~/proj", "dest/"], spawn=spawn)

    def member(self, name, kind=tarfile.REGTYPE, linkname="", mode=0o644):
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.mode = kind, linkname, mode
        return info

    def assert_refused(self, *members):
        with self.assertRaises(yo.YoError) as caught:
            self.pull_crafted((self.member("proj/ok"), b"1"), *members)
        self.assertEqual(os.listdir("dest"), [])
        return caught.exception.message

    def test_a_download_names_the_guest_path_it_reads(self):
        self.pull_crafted((self.member("proj/ok"), b"1"))
        self.assertEqual(self.said, ["yo: guest path :~/proj -> /home/agent/proj"])

    def test_a_fifo_is_refused_by_its_escaped_name(self):
        message = self.assert_refused(
            (self.member("proj/f\x1b[2J\nx", tarfile.FIFOTYPE), None)
        )
        self.assertIn('refusing to land "proj/f\\x1b[2J\\nx"', message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)

    def test_a_symlink_is_refused_even_when_it_points_inside_the_tree(self):
        message = self.assert_refused(
            (self.member("proj/lnk", tarfile.SYMTYPE, linkname="ok"), None)
        )
        self.assertIn('"proj/lnk": the guest sent a symlink', message)

    def test_a_hard_link_to_a_host_file_outside_the_tree_never_lands(self):
        Path("victim").write_text("precious")
        for target in (os.path.join(self.cwd, "victim"), "../../victim"):
            with self.subTest(target=target), self.assertRaises(yo.YoError):
                self.pull_crafted(
                    (self.member("proj/hl", tarfile.LNKTYPE, linkname=target), None)
                )
            self.assertEqual(os.stat("victim").st_nlink, 1)
            self.assertEqual(os.listdir("dest"), [])

    def test_a_staged_setuid_file_is_refused_whatever_the_host_tar_kept(self):
        os.makedirs("dest")

        def fill(staging):
            os.makedirs(os.path.join(staging, "proj"))
            path = os.path.join(staging, "proj", "run")
            Path(path).write_text("x")
            os.chmod(path, 0o4755)

        with self.assertRaisesRegex(yo.YoError, "a setuid or setgid file"):
            self.copy([":~/proj", "dest/"], spawn=self.filling_staging(fill))
        self.assertEqual(os.listdir("dest"), [])

    def test_a_host_tar_that_is_neither_bsdtar_nor_gnu_tar_is_refused(self):
        os.makedirs("dest")
        busybox = mock.Mock(returncode=0, stdout="BusyBox v1.36.1", stderr="")
        with (
            mock.patch.object(yo.proc, "run", return_value=busybox),
            self.assertRaisesRegex(yo.YoError, "neither bsdtar nor GNU tar"),
        ):
            self.copy([":~/proj", "dest/"], {"proj/a": "1"})
        self.assertEqual(os.listdir("dest"), [])


class TestCpFromGuestExcludesBuildOutput(CpFromGuestHarness):
    """node_modules, .venv, a Rust target and a Next.js .next dwarf the source
    and the project's tooling recreates them, so a pulled tree leaves them in
    the guest, at any depth, and says so. A target or .next is build output
    only beside its manifest: the name alone proves nothing."""

    def test_build_output_at_any_depth_is_left_in_the_guest_and_named(self):
        os.makedirs("dest")
        reported = self.pull(
            {
                "proj/Cargo.toml": "",
                "proj/package.json": "{}",
                "proj/node_modules/x/index.js": "js",
                "proj/.venv/bin/python": "py",
                "proj/target/debug/bin": "elf",
                "proj/.next/x": "built",
                "proj/web/deeper/node_modules/y": "js",
                "proj/web/src/a.py": "a",
            }
        )
        self.assertEqual(
            self.landed(),
            [
                "dest/proj/Cargo.toml",
                "dest/proj/package.json",
                "dest/proj/web/src/a.py",
            ],
        )
        self.assertFalse(os.path.lexists("dest/proj/web/deeper/node_modules"))
        for skipped in (
            "proj/node_modules",
            "proj/.venv",
            "proj/target",
            "proj/.next",
            "proj/web/deeper/node_modules",
        ):
            self.assertIn(
                'skipped "%s": build output is never copied' % skipped, reported
            )

    def test_a_target_or_next_without_its_manifest_lands_as_code(self):
        os.makedirs("dest")
        reported = self.pull({"proj/target/notes.md": "mine", "proj/web/.next/x": "y"})
        self.assertEqual(
            self.landed(), ["dest/proj/target/notes.md", "dest/proj/web/.next/x"]
        )
        self.assertNotIn("build output", reported)

    def test_a_source_the_operator_named_lands_even_when_it_is_build_output(self):
        os.makedirs("dest")
        self.pull({"proj/node_modules/a": "x"}, source=":~/proj/node_modules")
        self.assertEqual(self.landed(), ["dest/node_modules/a"])

    def test_build_output_already_on_the_host_is_left_untouched(self):
        os.makedirs("dest/proj/node_modules")
        Path("dest/proj/node_modules/keep").write_text("mine")
        self.pull({"proj/node_modules/new": "agent", "proj/ok": "1"})
        self.assertEqual(self.read("dest/proj/node_modules/keep"), "mine")
        self.assertFalse(os.path.lexists("dest/proj/node_modules/new"))
        self.assertEqual(self.read("dest/proj/ok"), "1")

    def test_a_control_path_beside_build_output_is_still_dropped(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/node_modules/x": "dep", "proj/.git/config": "evil", "proj/a": "1"}
        )
        self.assertEqual(self.landed(), ["dest/proj/a"])
        self.assertIn('skipped "proj/.git"', reported)


class TestCpFromGuestNeverWritesThroughSymlinks(CpFromGuestHarness):
    def test_a_symlink_inside_an_existing_destination_tree_is_not_written_through(
        self,
    ):
        os.makedirs("dest/proj")
        Path("victim").write_text("precious")
        os.symlink(os.path.join(self.cwd, "victim"), "dest/proj/notes")

        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/proj", "dest/"], {"proj/notes": "from the agent"})

        self.assertIn(
            os.path.join(self.cwd, "dest/proj/notes"), caught.exception.message
        )
        self.assertEqual(self.read("victim"), "precious")
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_refusal_names_a_hostile_entry_escaped(self):
        os.makedirs("dest/proj")
        os.symlink("/etc/hosts", "dest/proj/n\x1b[2J\nm")

        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/proj", "dest/"], {"proj/n\x1b[2J\nm": "x"})

        self.assertIn("dest/proj/n\\x1b[2J\\nm", caught.exception.message)
        self.assertNotIn("\x1b", caught.exception.message)
        self.assertNotIn("\n", caught.exception.message)

    def test_a_file_meeting_a_directory_is_refused_with_the_name_escaped(self):
        os.makedirs("dest/proj/n\x1b[2Jm")

        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/proj", "dest/"], {"proj/n\x1b[2Jm": "x"})

        self.assertIn("dest/proj/n\\x1b[2Jm", caught.exception.message)
        self.assertNotIn("\x1b", caught.exception.message)

    def test_a_move_that_fails_names_the_entry_escaped(self):
        os.makedirs("dest/proj")
        failure = OSError(errno.EACCES, "Permission denied")

        with mock.patch.object(yo.cp.os, "replace", side_effect=failure):
            with self.assertRaises(yo.YoError) as caught:
                self.copy([":~/proj", "dest/"], {"proj/n\x1b[2Jm": "x"})

        self.assertIn("dest/proj/n\\x1b[2Jm", caught.exception.message)
        self.assertNotIn("\x1b", caught.exception.message)

    def test_nothing_lands_when_any_entry_is_refused(self):
        os.makedirs("dest/proj")
        os.symlink("/etc/hosts", "dest/proj/b")

        with self.assertRaisesRegex(yo.YoError, "refusing to copy over symlink"):
            self.copy([":~/proj", "dest/"], {"proj/a": "1", "proj/b": "2"})

        self.assertFalse(os.path.lexists("dest/proj/a"))

    def test_a_top_level_symlink_at_the_destination_is_refused(self):
        os.makedirs("elsewhere")
        os.makedirs("dest")
        os.symlink(os.path.join(self.cwd, "elsewhere"), "dest/proj")

        with self.assertRaisesRegex(yo.YoError, "refusing to copy over symlink"):
            self.copy([":~/proj", "dest/"], {"proj/f": "x"})

        self.assertEqual(os.listdir("elsewhere"), [])

    def test_a_symlink_as_the_named_destination_file_is_refused(self):
        Path("victim").write_text("precious")
        os.symlink(os.path.join(self.cwd, "victim"), "renamed.txt")

        with self.assertRaisesRegex(yo.YoError, "refusing to copy over symlink"):
            self.copy([":~/x", "renamed.txt"], {"x": "from the agent"})

        self.assertEqual(self.read("victim"), "precious")

    def test_a_directory_lands_inside_an_existing_directory_and_merges(self):
        os.makedirs("dest/proj")
        Path("dest/proj/mine").write_text("keep")
        Path("dest/proj/shared").write_text("old")

        self.copy([":~/proj", "dest/"], {"proj/shared": "new", "proj/sub/f": "x"})

        self.assertEqual(self.read("dest/proj/mine"), "keep")
        self.assertEqual(self.read("dest/proj/shared"), "new")
        self.assertEqual(self.read("dest/proj/sub/f"), "x")
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_directory_copied_to_a_new_name_becomes_that_directory(self):
        self.copy([":~/proj", "fresh"], {"proj/f": "x"})

        self.assertEqual(self.read("fresh/f"), "x")
        self.assertEqual(self.leftovers("."), [])

    def test_a_single_file_copied_to_a_new_name_is_renamed(self):
        self.copy([":~/x", "renamed.txt"], {"x": "data"})

        self.assertEqual(self.read("renamed.txt"), "data")

    def test_a_single_file_replaces_an_existing_file(self):
        Path("renamed.txt").write_text("old")

        self.copy([":~/x", "renamed.txt"], {"x": "data"})

        self.assertEqual(self.read("renamed.txt"), "data")

    def test_several_sources_land_side_by_side_in_a_directory(self):
        os.makedirs("dest")

        self.copy([":~/a", ":~/b", "dest"], {"a": "1", "b": "2"})

        self.assertEqual(sorted(os.listdir("dest")), ["a", "b"])

    def test_several_sources_need_an_existing_directory(self):
        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/a", ":~/b", "nowhere"], {"a": "1", "b": "2"})

        self.assertIn("nowhere", caught.exception.message)
        self.assertEqual(self.guest_commands, [])

    def test_a_file_cannot_replace_a_directory(self):
        os.makedirs("dest/x")

        with self.assertRaisesRegex(yo.YoError, 'cannot copy over ".*/dest/x": it'):
            self.copy([":~/x", "dest/"], {"x": "data"})

        self.assertEqual(os.listdir("dest/x"), [])

    def test_staging_sits_beside_the_destination_on_the_same_filesystem(self):
        os.makedirs("dest")
        self.copy([":~/x", "dest/"], {"x": "data"})
        self.copy([":~/x", "newname"], {"x": "data"})

        self.assertEqual(
            os.path.dirname(self.unpacked_into[0]), os.path.join(self.cwd, "dest")
        )
        self.assertEqual(os.path.dirname(self.unpacked_into[1]), self.cwd)

    def test_staging_is_removed_when_the_transfer_fails(self):
        os.makedirs("dest")

        with self.assertRaisesRegex(yo.YoError, "cannot read /home/agent/x in the"):
            self.copy([":~/x", "dest/"], {})

        self.assertEqual(os.listdir("dest"), [])

    def test_a_read_only_guest_directory_is_still_cleaned_up_on_refusal(self):
        os.makedirs("dest/proj")
        os.symlink("/etc/hosts", "dest/proj/b")

        def fill(staging):
            tree = os.path.join(staging, "proj")
            os.makedirs(tree)
            Path(tree, "b").write_text("x")
            os.chmod(tree, 0o555)

        with self.assertRaisesRegex(yo.YoError, "refusing to copy over symlink"):
            self.copy([":~/proj", "dest/"], spawn=self.filling_staging(fill))

        self.assertEqual(self.leftovers("dest"), [])

    def making_staged(self, relative, mode):
        # A host tar that leaves one directory with the given mode, as -p would
        # for a guest directory the agent chmodded.
        def fill(staging):
            tree = os.path.join(staging, relative)
            os.makedirs(tree)
            os.chmod(tree, mode)

        return fill

    def test_an_unreadable_staged_subdirectory_is_cleaned_up(self):
        os.makedirs("dest")
        spawn = self.filling_staging(self.making_staged("proj/sub", 0o300), fails=True)

        with self.assertRaisesRegex(yo.YoError, "cannot unpack the download"):
            self.copy([":~/proj", "dest/"], spawn=spawn)

        self.assertEqual(self.leftovers("dest"), [])

    def test_an_unreadable_staged_subdirectory_does_not_break_a_good_copy(self):
        os.makedirs("dest")

        self.addCleanup(os.chmod, os.path.join(self.cwd, "dest/proj/sub"), 0o700)

        spawn = self.filling_staging(self.making_staged("proj/sub", 0o300))
        self.copy([":~/proj", "dest/"], spawn=spawn)

        self.assertEqual(self.leftovers("dest"), [])
        self.assertTrue(os.path.isdir("dest/proj/sub"))

    def test_a_cleanup_failure_is_reported_next_to_the_original_error(self):
        os.makedirs("dest")

        with (
            mock.patch.object(yo.cp, "remove_staging", side_effect=yo.YoError("stuck")),
            self.assertRaises(yo.YoError) as caught,
        ):
            self.copy([":~/proj", "dest/"], {})

        self.assertIn("cannot read /home/agent/proj", caught.exception.message)
        self.assertIn("stuck", "\n".join(self.said))

    def test_a_move_the_mac_refuses_is_a_yo_error_counting_what_landed(self):
        os.makedirs("dest")

        with (
            mock.patch.object(
                yo.os, "replace", side_effect=PermissionError(13, "Permission denied")
            ),
            self.assertRaises(yo.YoError) as caught,
        ):
            self.copy([":~/a", ":~/b", "dest"], {"a": "1", "b": "2"})

        self.assertIn("Permission denied", caught.exception.message)
        self.assertIn("0 of 2 entries already landed", caught.exception.message)
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_trailing_slash_on_a_missing_directory_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/x", "newdir/"], {"x": "data"})

        self.assertIn(
            '"%s/" is not a directory' % os.path.join(self.cwd, "newdir"),
            caught.exception.message,
        )
        self.assertEqual(self.guest_commands, [])
        self.assertFalse(os.path.lexists("newdir"))

    def test_a_trailing_slash_on_a_file_is_refused(self):
        Path("afile").write_text("keep")

        with self.assertRaisesRegex(yo.YoError, 'destination ".*/afile/" is not a'):
            self.copy([":~/x", "afile/"], {"x": "data"})

        self.assertEqual(self.read("afile"), "keep")

    def test_a_missing_destination_parent_is_a_yo_error(self):
        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/x", "no/such/parent"], {"x": "data"})

        self.assertIn("no/such", caught.exception.message)

    def test_a_destination_with_a_newline_cannot_forge_a_message_line(self):
        for operands in (
            [":~/x", "new\nyo: forged/"],
            [":~/x", ":~/y", "new\nyo: forged"],
            [":~/x", "new\nyo: forged/parent"],
        ):
            with self.subTest(operands=operands):
                with self.assertRaises(yo.YoError) as caught:
                    self.copy(operands, {"x": "1", "y": "2"})
                self.assertIn("new\\nyo: forged", caught.exception.message)
                self.assertNotIn("\n", caught.exception.message)

    def test_a_staging_directory_that_stays_cannot_forge_a_message_line(self):
        os.makedirs("d\nyo: forged")
        with (
            mock.patch.object(
                yo.cp, "remove_tree", side_effect=PermissionError(13, "Denied")
            ),
            self.assertRaises(yo.YoError) as caught,
        ):
            self.copy([":~/x", "d\nyo: forged"], {"x": "1"})
        self.assertIn("d\\nyo: forged/.yo-cp-", caught.exception.message)
        self.assertNotIn("\n", caught.exception.message)


class TestCpFromGuestNeverLandsControlPaths(CpFromGuestHarness):
    """A pulled .git/config (fsmonitor, pager, core.hooksPath) or .envrc runs
    as the operator on the next git or shell command, so none of them lands."""

    def test_control_paths_are_skipped_and_reported_but_ordinary_files_land(self):
        os.makedirs("dest/proj")
        Path("dest/proj/main.py").write_text("old")
        reported = self.pull(
            {
                "proj/.git/config": "[core]\nfsmonitor = evil",
                "proj/.envrc": "evil",
                "proj/.vscode/settings.json": "{}",
                "proj/main.py": "new",
                "proj/src/lib.py": "lib",
            }
        )
        self.assertEqual(self.landed(), ["dest/proj/main.py", "dest/proj/src/lib.py"])
        self.assertEqual(self.read("dest/proj/main.py"), "new")
        for skipped in (".git", ".envrc", ".vscode"):
            self.assertIn('skipped "proj/%s"' % skipped, reported)
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_hostile_directory_name_is_reported_escaped(self):
        os.makedirs("dest")
        reported = self.pull(
            {
                "proj/a\x1b]0;pwned\x07\nFAIL: forged\r/.git/config": "evil",
                "proj/ok": "ok",
            }
        )
        self.assertIn(
            'skipped "proj/a\\x1b]0;pwned\\x07\\nFAIL: forged\\r/.git"', reported
        )
        self.assertNotIn("\x1b", reported)
        self.assertEqual(self.unprefixed_lines(reported), [])

    def test_a_control_path_that_cannot_be_dropped_is_named_escaped(self):
        os.makedirs("dest")
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if path.endswith(".git"):
                raise OSError(errno.EPERM, "Operation not permitted")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(yo.cp.os, "unlink", unlink):
            with self.assertRaises(yo.YoError) as caught:
                self.pull({"proj/a\x1b[2Jb/.git": "gitdir: x"})

        self.assertIn("proj/a\\x1b[2Jb/.git", caught.exception.message)
        self.assertNotIn("\x1b", caught.exception.message)

    def test_control_names_are_matched_case_insensitively(self):
        # APFS folds .GIT into .git, so the spelling must not matter.
        os.makedirs("dest")
        reported = self.pull({"proj/.GIT/config": "evil", "proj/Ok.txt": "ok"})
        self.assertEqual(self.landed(), ["dest/proj/Ok.txt"])
        self.assertIn('skipped "proj/.GIT"', reported)

    def test_a_nested_control_path_in_a_tree_new_to_the_host_is_caught(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/a/.git/hooks/pre-commit": "evil", "proj/a/ok.txt": "ok"}
        )
        self.assertEqual(self.landed(), ["dest/proj/a/ok.txt"])
        self.assertIn('skipped "proj/a/.git"', reported)

    def test_a_control_name_that_is_only_a_prefix_is_ordinary(self):
        os.makedirs("dest")
        self.pull({"proj/.gitignore": "x", "proj/.github/ci.yml": "y"})
        self.assertEqual(
            self.landed(), ["dest/proj/.github/ci.yml", "dest/proj/.gitignore"]
        )

    def test_a_control_file_on_the_host_is_never_overwritten(self):
        os.makedirs("dest/proj/.git")
        Path("dest/proj/.git/config").write_text("[core]\nhooksPath = mine")
        reported = self.pull({"proj/.git/config": "evil", "proj/ok.txt": "ok"})
        self.assertEqual(self.read("dest/proj/.git/config"), "[core]\nhooksPath = mine")
        self.assertEqual(self.read("dest/proj/ok.txt"), "ok")
        self.assertIn('skipped "proj/.git"', reported)

    def test_every_version_control_hook_editor_and_direnv_name_is_skipped(self):
        for name in (".hg", ".svn", ".jj", ".husky", ".idea", ".ENVRC", ".Vscode"):
            with self.subTest(name=name):
                shutil.rmtree("dest", ignore_errors=True)
                os.makedirs("dest")
                reported = self.pull({"proj/%s/x" % name: "evil", "proj/ok": "ok"})
                self.assertEqual(self.landed(), ["dest/proj/ok"])
                self.assertIn('skipped "proj/%s"' % name, reported)

    def test_names_that_apfs_folds_to_a_control_name_are_skipped(self):
        # U+017F (long s) folds to "s" on APFS, but str.lower() leaves it alone.
        os.makedirs("dest")
        reported = self.pull({"proj/.vſcode/settings.json": "{}", "proj/ok": "ok"})
        self.assertEqual(self.landed(), ["dest/proj/ok"])
        self.assertIn('skipped "proj/.vſcode"', reported)

    def test_a_gitfile_is_skipped_like_a_git_directory(self):
        os.makedirs("dest")
        reported = self.pull({"proj/.git": "gitdir: ../x", "proj/ok": "ok"})
        self.assertEqual(self.landed(), ["dest/proj/ok"])
        self.assertIn('skipped "proj/.git"', reported)

    def test_names_that_merely_resemble_control_names_land_as_ordinary_files(self):
        os.makedirs("dest")
        names = [".gitx", "x.git", ".envrc.bak", ".gitmodules"]
        self.pull({"proj/%s" % name: "data" for name in names})
        self.assertEqual(self.landed(), sorted("dest/proj/%s" % name for name in names))

    def test_a_control_path_inside_a_control_path_is_reported_once(self):
        os.makedirs("dest")
        reported = self.pull({"proj/.git/.git/x": "evil", "proj/ok": "ok"})
        self.assertEqual(self.landed(), ["dest/proj/ok"])
        self.assertEqual(reported.count('skipped "proj/.git"'), 1)
        self.assertNotIn("proj/.git/.git", reported)

    def test_a_download_source_that_is_a_control_path_lands_nothing(self):
        os.makedirs("dest")
        reported = self.pull({"proj/.git/config": "evil"}, source=":~/proj/.git")
        self.assertEqual(self.landed(), [])
        self.assertIn('skipped ".git"', reported)
        self.assertEqual(self.leftovers("dest"), [])


class TestCpFromGuestFailsWithAMessage(CpFromGuestHarness):
    """A Mac-side failure while staging or landing is a message that names the
    file and the reason, never a traceback, and the staging directory goes."""

    def test_an_os_error_while_landing_is_a_message_and_staging_is_removed(self):
        os.makedirs("dest")
        real_listdir = os.listdir

        def listdir(path="."):
            if ".yo-cp-" in str(path):
                raise OSError(errno.EIO, "Input/output error", path)
            return real_listdir(path)

        with mock.patch.object(yo.cp.os, "listdir", listdir):
            with self.assertRaises(yo.YoError) as caught:
                self.pull({"proj/a\x1b[2J": "1"})
        message = caught.exception.message
        self.assertIn("the copy failed: ", message)
        self.assertIn("/dest/.yo-cp-", message)
        self.assertIn("Input/output error", message)
        self.assertNotIn("\x1b", message)
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_cleanup_failure_that_is_no_os_error_joins_the_copys_own_error(self):
        os.makedirs("dest")
        with (
            mock.patch.object(
                yo.cp.shutil,
                "rmtree",
                side_effect=RecursionError("maximum recursion depth exceeded"),
            ),
            self.assertRaises(yo.YoError) as caught,
        ):
            self.copy([":~/proj", "dest/"], {})
        said = "\n".join(self.said)
        self.assertIn("cannot read /home/agent/proj", caught.exception.message)
        self.assertIn("cannot remove the staging directory", said)
        self.assertIn("maximum recursion depth exceeded", said)

    def test_a_tree_deeper_than_the_frame_limit_is_unlocked(self):
        # A low frame limit keeps the deepest path under macOS's 1024-byte PATH_MAX.
        limit = sys.getrecursionlimit()
        self.addCleanup(sys.setrecursionlimit, limit)
        sys.setrecursionlimit(200)
        dirs = [os.path.join(self.cwd, "deep")]
        for _ in range(sys.getrecursionlimit() + 100):
            dirs.append(os.path.join(dirs[-1], "d"))
        for directory in dirs:
            os.mkdir(directory)
        # Bottom up by hand: shutil.rmtree recurses too, on Python 3.9.
        self.addCleanup(lambda: [os.rmdir(d) for d in reversed(dirs)])
        os.chmod(dirs[-1], 0)
        yo.unlock_tree(dirs[0])
        self.assertEqual(stat.S_IMODE(os.stat(dirs[-1]).st_mode) & 0o700, 0o700)


class TestCpRefusesSymlinks(CpHarness):
    def refused(self, paths, message_part):
        with self.assertRaises(yo.YoError) as caught:
            self.run_cp(paths)
        self.assertIn(message_part, caught.exception.message)

    def test_a_symlink_source_is_refused_by_name(self):
        os.symlink("a.txt", "link")
        self.refused(["link", ":~/x"], os.path.join(self.cwd, "link"))

    def test_a_symlink_inside_a_source_directory_is_refused_by_name(self):
        os.makedirs("repo/sub")
        os.symlink("/etc/passwd", "repo/sub/leak")
        self.refused(["repo", ":~/x"], os.path.join(self.cwd, "repo/sub/leak"))

    def test_a_symlink_to_a_directory_inside_a_source_is_refused(self):
        os.makedirs("repo")
        os.symlink("/etc", "repo/etc")
        self.refused(["repo", ":~/x"], "repo/etc")

    def test_the_message_says_scp_would_copy_the_target(self):
        os.symlink("/etc/passwd", "link")
        self.refused(["link", ":~/x"], "scp would copy its target")

    def test_nothing_reaches_the_guest_when_a_source_is_refused(self):
        os.symlink("a.txt", "link")
        calls = {"run": [], "ssh_run": []}

        def fake_run(argv, **kwargs):
            calls["run"].append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            calls["ssh_run"].append(list(argv))
            stdout, _ = ssh_ends(argv, None, 0, stdout=b"")
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
            mock.patch.object(yo.ui, "err"),
            self.assertRaisesRegex(yo.YoError, "refusing to upload symlink"),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", "a.txt", "link", ":~/x"]))
        self.assertEqual(calls, {"run": [], "ssh_run": [AGENT_LOGIN]})

    def test_a_trailing_slash_does_not_hide_a_symlink_source(self):
        os.makedirs("real")
        Path("real/secret").write_text("x")
        os.symlink("real", "lnk")
        self.refused(["lnk/", ":~/x"], os.path.join(self.cwd, "lnk"))

    def test_a_symlink_typed_as_a_parent_component_is_refused(self):
        # lstat judges the last component only, so "lnk/." and "lnk/secret"
        # resolve the link, and so does "lnk/../real/x".
        os.makedirs("real/sub")
        Path("real/secret").write_text("x")
        os.symlink("real", "lnk")
        for spelling in ("lnk/.", "lnk/secret", "lnk/sub", "lnk/../real/secret"):
            with self.subTest(spelling=spelling):
                self.refused([spelling, ":~/x"], os.path.join(self.cwd, "lnk"))
        self.refused([os.path.join(self.cwd, "lnk/secret"), ":~/x"], "lnk")

    def test_a_missing_source_is_refused_naming_it(self):
        self.refused(["nope", ":~/x"], os.path.join(self.cwd, "nope"))

    def test_a_name_with_a_newline_cannot_forge_a_message_line(self):
        forged = "x\nyo: forged line"
        os.symlink("t\nyo: forged target", forged)
        with self.assertRaises(yo.YoError) as caught:
            self.run_cp([forged, ":~/x"])
        message = caught.exception.message
        self.assertNotIn("\n", message)
        self.assertIn("x\\nyo: forged line", message)
        self.assertIn("t\\nyo: forged target", message)

    def test_a_missing_source_with_a_newline_cannot_forge_a_message_line(self):
        with self.assertRaises(yo.YoError) as caught:
            self.run_cp(["gone\nyo: forged", ":~/x"])
        self.assertNotIn("\n", caught.exception.message)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 directory")
    def test_an_unreadable_directory_with_a_newline_cannot_forge_a_message_line(self):
        os.makedirs("repo/d\nyo: forged")
        os.chmod("repo/d\nyo: forged", 0)
        self.addCleanup(os.chmod, "repo/d\nyo: forged", 0o700)
        with self.assertRaises(yo.YoError) as caught:
            self.run_cp(["repo", ":~/x"])
        self.assertIn("Permission denied", caught.exception.message)
        self.assertNotIn("\n", caught.exception.message)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 directory")
    def test_a_download_target_that_cannot_be_read_cannot_forge_a_message_line(self):
        os.makedirs("dest")
        os.chmod("dest", 0)
        self.addCleanup(os.chmod, "dest", 0o700)
        with self.assertRaises(yo.YoError) as caught:
            yo.plan_merge("staged", "dest/e\nyo: forged", [])
        self.assertNotIn("\n", caught.exception.message)

    def test_a_directory_without_symlinks_is_uploaded(self):
        os.makedirs("repo/sub")
        Path("repo/sub/f").write_text("x")
        argv = self.run_cp(["repo", ":~/x"])["run"]
        self.assertIn(os.path.join(self.cwd, "repo"), argv)


class TestHome(unittest.TestCase):
    def test_home_is_normalised_so_it_equals_a_path_built_from_it(self):
        for spelled in ("/Users/x/", "/Users/x//", "/Users/./x"):
            with self.subTest(home=spelled), with_home(spelled):
                self.assertEqual(yo.home(), "/Users/x")


@needs_tool("ssh")
class TestSshConfigReachesBox(FakeHome):
    """Real `ssh -G` against a ~/.ssh/config in the fake home. Include paths are
    absolute, so the tests hold whichever home an ssh expands ~ against (OpenSSH
    10.2 takes $HOME, the fake one; older ones may take pw_dir)."""

    def setUp(self):
        super().setUp()
        self.instance_cfg = yo.ssh_cfg()
        os.makedirs(os.path.dirname(self.instance_cfg))
        with open(self.instance_cfg, "w") as handle:
            handle.write(
                'Host %s\n  HostKeyAlias %s\n  UserKnownHostsFile "%s"\n'
                % (yo.box.vm_host, yo.box.vm_host, yo.known_hosts_path())
            )
        self.config = os.path.join(self.home, ".ssh", "config")

    def write_config(self, text, mode=0o600):
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w") as handle:
            handle.write(text)
        os.chmod(self.config, mode)

    def test_no_config_does_not_reach_the_box(self):
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_the_per_instance_line_reaches_it(self):
        self.write_config("Include %s\n" % self.instance_cfg)
        self.assertTrue(yo.ssh_config_reaches_box())

    def test_the_glob_for_both_hosts_reaches_it(self):
        self.write_config(
            yo.SSH_CONFIG_INCLUDE_LINE.replace("~", self.home) + "\nHost other\n"
        )
        self.assertTrue(yo.ssh_config_reaches_box())

    def test_an_include_naming_two_paths_reaches_it(self):
        self.write_config("Include /nonexistent/x.config %s\n" % self.instance_cfg)
        self.assertTrue(yo.ssh_config_reaches_box())

    @unittest.skipIf(os.getuid() == 0, "root owns everything ssh would accept")
    def test_a_config_owned_by_another_account_is_an_error_naming_chown(self):
        self.write_config("Include %s\n" % self.instance_cfg)
        with mock.patch.object(yo.sshconfig.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(yo.YoError) as caught:
                yo.ssh_config_reaches_box()
        self.assertIn("bad owner", caught.exception.message)
        self.assertIn(
            "sudo chown %d %s" % (os.getuid() + 1, self.config),
            caught.exception.message,
        )
        self.assertNotIn("chmod", caught.exception.message)

    def test_an_earlier_block_that_widens_the_pin_does_not(self):
        # ssh keeps the first value: here the pin plus another file, not ours.
        self.write_config(
            'Host *\n  UserKnownHostsFile "%s" /tmp/other\nInclude %s\n'
            % (yo.known_hosts_path(), self.instance_cfg)
        )
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_an_earlier_block_that_renames_the_pin_does_not(self):
        # The pin file is the right one, but ssh would look in it for another name.
        self.write_config(
            "Host *\n  HostKeyAlias other\nInclude %s\n" % self.instance_cfg
        )
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_a_commented_line_does_not(self):
        self.write_config("# Include %s\n" % self.instance_cfg)
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_an_include_after_a_host_block_applies_only_to_that_block(self):
        self.write_config("Host other\n  User x\nInclude %s\n" % self.instance_cfg)
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_a_path_relative_to_home_misses_because_ssh_reads_it_from_ssh_dir(self):
        # `Include .lima/yolobox/ssh.config` reads ~/.ssh/.lima/..., not ~/.lima/...
        relative = os.path.relpath(self.instance_cfg, self.home)
        self.write_config("Include %s\n" % relative)
        self.assertFalse(yo.ssh_config_reaches_box())

    def test_a_config_ssh_refuses_is_an_error_naming_why(self):
        for text, mode, needle in (
            ("Lizard yes\n", 0o600, "Bad configuration option"),
            ("Include %s\n" % self.instance_cfg, 0o664, "chmod go-w"),
            ("Include %s\n" % self.instance_cfg, 0o602, "chmod go-w"),
        ):
            with self.subTest(needle=needle):
                self.write_config(text, mode)
                with self.assertRaises(yo.YoError) as caught:
                    yo.ssh_config_reaches_box()
                self.assertIn(
                    "ssh cannot read %s" % self.config, caught.exception.message
                )
                self.assertIn(needle, caught.exception.message)


def proc_read(path):
    try:
        with open(path, errors="replace") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def isatty(value):
    fake_stdin = mock.Mock()
    fake_stdin.isatty.return_value = value
    return mock.patch.object(yo.sys, "stdin", fake_stdin)


INCLUDE = yo.SSH_CONFIG_INCLUDE_LINE + "\n"


class TestEnsureSshConfigInclude(FakeHome):
    """Prompting and writing; whether ssh then reaches the box is
    TestSshConfigReachesBox's, so here it reaches it once the line is there."""

    def setUp(self):
        super().setUp()
        for name in ("err", "out"):
            patcher = mock.patch.object(yo.ui, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        def reaches():
            # As the real check: a file ssh cannot read is an error.
            try:
                text = proc_read(self.config_path())
            except PermissionError as failure:
                raise yo.YoError(
                    "ssh cannot read %s: %s" % (self.config_path(), failure)
                )
            return text is not None and yo.SSH_CONFIG_INCLUDE_LINE in text

        patcher = mock.patch.object(yo.sshconfig, "ssh_config_reaches_box", reaches)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config_path(self):
        return os.path.join(self.home, ".ssh", "config")

    def write_config(self, text):
        ssh_dir = os.path.join(self.home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(self.config_path(), "w") as handle:
            handle.write(text)
        return self.config_path()

    def read_config(self):
        with open(self.config_path(), "r") as handle:
            return handle.read()

    def test_a_line_another_yo_added_after_the_prompt_is_not_added_again(self):
        # Two yo bootstraps prompted at once and both were answered y.
        self.write_config("Host other\n")
        yo.sshconfig.add_ssh_config_include()
        yo.sshconfig.add_ssh_config_include()
        self.assertEqual(self.read_config().count(yo.SSH_CONFIG_INCLUDE_LINE), 1)

    def test_already_present_is_left_untouched(self):
        self.write_config(INCLUDE)
        with isatty(True):
            yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(self.read_config(), INCLUDE)

    def test_accepted_prepends_at_the_top(self):
        self.write_config("Host other\n  User x\n")
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(
            self.read_config(), yo.SSH_CONFIG_INCLUDE_LINE + "\nHost other\n  User x\n"
        )

    def test_accepted_creates_ssh_dir_and_config_when_absent(self):
        with isatty(True):
            with mock.patch("builtins.input", return_value="yes"):
                yo.ensure_ssh_config_include(refuse=True)
        ssh_dir = os.path.join(self.home, ".ssh")
        self.assertEqual(stat.S_IMODE(os.stat(ssh_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path()).st_mode), 0o600)
        self.assertEqual(self.read_config(), yo.SSH_CONFIG_INCLUDE_LINE + "\n")

    def test_a_dangling_config_symlink_is_a_yo_error_naming_the_target(self):
        os.makedirs(os.path.join(self.home, ".ssh"))
        target = os.path.join(self.home, "gone", "ssh_config")
        os.symlink(target, self.config_path())
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertIn(target, caught.exception.message)

    def test_accepted_writes_through_a_symlinked_config(self):
        kept = os.path.join(self.home, "kept")
        os.makedirs(kept)
        target = os.path.join(kept, "ssh_config")
        with open(target, "w") as handle:
            handle.write("Host other\n")
        os.makedirs(os.path.join(self.home, ".ssh"))
        os.symlink(target, self.config_path())
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
        self.assertTrue(os.path.islink(self.config_path()))
        with open(target) as handle:
            self.assertEqual(
                handle.read(), yo.SSH_CONFIG_INCLUDE_LINE + "\nHost other\n"
            )

    def test_accepted_keeps_the_existing_mode(self):
        path = self.write_config("Host other\n")
        os.chmod(path, 0o644)
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_accepted_keeps_undecodable_bytes(self):
        path = self.write_config("")
        with open(path, "wb") as handle:
            handle.write(b"# caf\xe9\nHost other\n")
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
        with open(path, "rb") as handle:
            self.assertEqual(
                handle.read(),
                yo.SSH_CONFIG_INCLUDE_LINE.encode() + b"\n# caf\xe9\nHost other\n",
            )

    def test_an_unreadable_config_is_an_error_not_an_empty_file(self):
        path = self.write_config("Host other\n")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                with self.assertRaises(yo.YoError) as raised:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertIn("cannot read %s" % path, raised.exception.message)
        os.chmod(path, 0o600)
        with open(path) as handle:
            self.assertEqual(handle.read(), "Host other\n")

    def test_accepted_a_second_time_does_not_duplicate(self):
        self.write_config("Host other\n")
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
                yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(self.read_config().count(yo.SSH_CONFIG_INCLUDE_LINE), 1)

    def test_declined_leaves_the_file_untouched_and_refuses(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(True):
            with mock.patch("builtins.input", return_value="n"):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.read_config(), original)

    def test_declined_without_refuse_returns(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(True):
            with mock.patch("builtins.input", return_value="n"):
                present = yo.ensure_ssh_config_include(refuse=False)
        self.assertFalse(present)
        self.assertEqual(self.read_config(), original)

    def test_the_answer_says_whether_the_include_is_in_place(self):
        self.write_config(INCLUDE)
        self.assertTrue(yo.ensure_ssh_config_include(refuse=False))

    def test_end_of_input_at_the_prompt_is_a_no(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(True):
            with mock.patch("builtins.input", side_effect=EOFError):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_ssh_config_include(refuse=True)
                self.assertEqual(
                    (caught.exception.message, caught.exception.code), (None, 1)
                )
        self.assertEqual(self.read_config(), original)

    def test_non_tty_never_prompts_and_refuses(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(False):
            with mock.patch(
                "builtins.input", side_effect=AssertionError("must not prompt")
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.read_config(), original)

    def test_non_tty_without_refuse_returns(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(False):
            with mock.patch(
                "builtins.input", side_effect=AssertionError("must not prompt")
            ):
                yo.ensure_ssh_config_include(refuse=False)
        self.assertEqual(self.read_config(), original)


LIMA_BLOCK = (
    "Host lima-yolobox\n"
    '  IdentityFile "/Users/user/.lima/_config/user"\n'
    "  StrictHostKeyChecking no\n"
    "  User user\n"
    "  ControlMaster auto\n"
    '  ControlPath "/Users/user/.lima/yolobox/ssh.sock"\n'
    "  ControlPersist yes\n"
    "  Hostname 127.0.0.1\n"
    "  Port 60022\n"
)


class SshConfigCase(FakeHome):
    """The machine's ssh.config written from the driver's facts (Lima's, unless
    a test says otherwise), and what a real ssh makes of it."""

    facts = LIMA_FACTS

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(yo.LIMA, "ssh_facts", return_value=self.facts)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(yo.instance_dir())

    def write(self):
        yo.sshconfig.write_ssh_config()
        return self.read_config()

    def read_config(self):
        with open(yo.ssh_cfg(), "r") as handle:
            return handle.read()

    def effective(self, argv, host=None):
        completed = subprocess.run(
            list(argv) + ["-G", host or yo.box.vm_host],
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(line.split(" ", 1) for line in completed.stdout.splitlines())


class TestWriteSshConfig(SshConfigCase):
    def test_the_file_is_private_and_a_second_write_changes_nothing(self):
        first = self.write()
        self.assertEqual(stat.S_IMODE(os.stat(yo.ssh_cfg()).st_mode), 0o600)
        with mock.patch.object(yo.proc, "atomic_write") as write:
            yo.sshconfig.write_ssh_config()
        write.assert_not_called()
        self.assertEqual(self.read_config(), first)

    def test_the_file_lima_regenerates_at_a_start_is_put_back(self):
        wanted = self.write()
        with open(yo.ssh_cfg(), "w") as handle:
            handle.write(LIMA_BLOCK)
        self.assertEqual(self.write(), wanted)

    def test_the_operators_block_names_the_facts_and_pins_the_key(self):
        text = self.write()
        block = "\n" + text.split("Host lima-yolobox\n", 1)[1].split("\n\n", 1)[0]
        for line in (
            "Hostname 127.0.0.1",
            "Port 60022",
            "User user",
            'IdentityFile "/Users/user/.lima/_config/user"',
            'IdentityAgent "none"',
            "IdentitiesOnly yes",
            "ForwardAgent no",
            "ControlMaster auto",
            'ControlPath "%s"' % os.path.join(yo.instance_dir(), "yo-op.sock"),
            "HostKeyAlias lima-yolobox",
            'UserKnownHostsFile "%s"' % yo.known_hosts_path(),
            "StrictHostKeyChecking accept-new",
            "NoHostAuthenticationForLocalhost no",
        ):
            self.assertIn("\n  " + line + "\n", block + "\n", line)

    def test_both_blocks_pin_under_the_one_alias(self):
        aliases = re.findall(r"^\s*HostKeyAlias (.*)$", self.write(), re.M)
        self.assertEqual(aliases, [yo.box.vm_host] * 2)

    def test_the_agent_role_gets_its_own_master_above_the_operators_block(self):
        text = self.write()
        match = text.split("Match host lima-yolobox user agent\n", 1)[1]
        self.assertIn('  ControlPath "%s"' % yo.agent_control_path(), match)
        self.assertLess(text.index("ssh-agent.sock"), text.index("yo-op.sock"))

    def test_no_role_rides_the_master_lima_keeps_at_ssh_sock(self):
        # Lima's hostagent opened that master without yo's forwards.
        text = self.write()
        paths = re.findall(r'^\s*ControlPath "([^"]*)"$', text, re.M)
        for role in (yo.OPERATOR, yo.AGENT, yo.OPERATOR_FIRST_CONTACT):
            argv = yo.sshx.control_argv(role, "check")
            paths += [
                o.split("=", 1)[1].strip('"')
                for o in argv
                if o.startswith("ControlPath=")
            ]
        self.assertGreaterEqual(len(paths), 4, paths)
        self.assertNotIn(os.path.join(yo.instance_dir(), "ssh.sock"), paths)

    def test_herdrs_alias_is_the_agent_with_no_shared_master_and_the_same_facts(self):
        text = self.write()
        alias = text.split("Host yolobox\n", 1)[1]
        for line in (
            "User agent",
            'ControlPath "none"',
            "ForwardAgent no",
            "Hostname 127.0.0.1",
            "Port 60022",
            'IdentityFile "/Users/user/.lima/_config/user"',
            "HostKeyAlias lima-yolobox",
            'UserKnownHostsFile "%s"' % yo.known_hosts_path(),
            "NoHostAuthenticationForLocalhost no",
        ):
            self.assertIn("  " + line + "\n", alias, line)
        self.assertNotIn("StrictHostKeyChecking no", alias)

    def test_an_identity_held_by_an_agent_is_named_for_both_names(self):
        self.facts = yo.SshFacts(61234, "alice", "/h/box/operator.pub", "/h/agent.sock")
        with mock.patch.object(yo.LIMA, "ssh_facts", return_value=self.facts):
            text = self.write()
        self.assertEqual(text.count('  IdentityAgent "/h/agent.sock"\n'), 2)
        self.assertEqual(text.count('  IdentityFile "/h/box/operator.pub"\n'), 2)
        self.assertEqual(text.count("  Port 61234\n"), 2)

    def test_a_path_with_a_double_quote_cannot_be_written(self):
        with mock.patch.object(
            yo.LIMA, "ssh_facts", return_value=yo.SshFacts(1, "u", '/k"ey', "none")
        ):
            with self.assertRaisesRegex(yo.YoError, "double quote"):
                yo.sshconfig.write_ssh_config()

    @needs_tool("ssh")
    def test_no_role_forwards_an_agent_whatever_a_later_block_says(self):
        # A user's own "Host lima-yolobox / ForwardAgent yes" sits after the
        # Include; ssh keeps the first value. Only `yo ssh` forwards, by its
        # own command line.
        self.write()
        with open(yo.ssh_cfg(), "a") as handle:
            handle.write("\nHost lima-yolobox\n  ForwardAgent yes\n")
        for role in (yo.OPERATOR, yo.AGENT):
            with self.subTest(role=role.name):
                argv = ["ssh"] + yo.ssh_options(role)
                self.assertEqual(self.effective(argv)["forwardagent"], "no")
        argv = ["ssh"] + yo.ssh_options(yo.OPERATOR) + yo.forward_agent_opts()
        effective = self.effective(argv)
        self.assertEqual(effective["forwardagent"], yo.LIMA.op_sock())
        # ssh asks for the forward only while it holds an agent itself: with
        # IdentityAgent none it silently forwards nothing.
        self.assertEqual(effective["identityagent"], yo.LIMA.op_sock())
        # The login still offers only the instance's own key, so 1Password
        # signs nothing for it.
        self.assertEqual(effective["identitiesonly"], "yes")

    @needs_tool("ssh")
    def test_every_connection_gives_up_on_a_guest_that_stops_answering(self):
        self.write()
        later = "  ServerAliveInterval 0\n  ConnectTimeout 0\n"
        with open(yo.ssh_cfg(), "a") as handle:
            handle.write("\nHost lima-yolobox\n" + later + "\nHost yolobox\n" + later)
        for user, host in (
            ("agent", "lima-yolobox"),
            ("user", "lima-yolobox"),
            ("agent", "yolobox"),
        ):
            with self.subTest(user=user, host=host):
                resolved = self.effective(
                    ["ssh", "-F", yo.ssh_cfg(), "-o", "User=" + user], host
                )
                self.assertEqual(resolved["connecttimeout"], "30")
                self.assertEqual(resolved["serveraliveinterval"], "15")
                self.assertEqual(resolved["serveralivecountmax"], "8")

    @needs_tool("ssh")
    def test_the_agent_role_rides_its_own_master(self):
        self.write()
        resolved = self.effective(["ssh"] + yo.ssh_options(yo.AGENT, mux=True))
        self.assertEqual(resolved["controlpath"], yo.agent_control_path())
        resolved = self.effective(["ssh", "-F", yo.ssh_cfg(), "-o", "User=agent"])
        self.assertEqual(resolved["controlpath"], yo.agent_control_path())


class TestHostKeyPinning(SshConfigCase):
    PINNED = {
        "stricthostkeychecking": "accept-new",
        "nohostauthenticationforlocalhost": "no",
    }

    def pinned_keywords(self, argv, host=None):
        wanted = (
            "stricthostkeychecking",
            "userknownhostsfile",
            "nohostauth",
            "hostkeyalias",
        )
        return {
            key: value
            for key, value in self.effective(argv, host).items()
            if key.startswith(wanted)
        }

    def assert_pinned(self, effective):
        for keyword, value in self.PINNED.items():
            self.assertEqual(effective[keyword], value)
        self.assertEqual(effective["userknownhostsfile"], yo.known_hosts_path())
        self.assertEqual(effective["hostkeyalias"], yo.box.vm_host)

    @needs_tool("ssh")
    def test_every_yo_role_pins_the_key(self):
        self.write()
        for role in (yo.OPERATOR, yo.AGENT):
            with self.subTest(role=role.name):
                self.assert_pinned(self.pinned_keywords(["ssh"] + yo.ssh_options(role)))

    @needs_tool("ssh")
    def test_a_bare_ssh_through_the_written_file_pins_the_key_for_both_names(self):
        self.write()
        for user, host in (([], None), (["-o", "User=agent"], None), ([], "yolobox")):
            with self.subTest(user=user, host=host):
                argv = ["ssh", "-F", yo.ssh_cfg()] + user
                self.assert_pinned(self.pinned_keywords(argv, host))

    @needs_tool("ssh")
    def test_editors_reach_the_pin_through_the_users_include(self):
        self.write()
        user_config = os.path.join(self.home, ".ssh", "config")
        os.makedirs(os.path.dirname(user_config))
        with open(user_config, "w") as handle:
            handle.write("Include %s\n" % yo.ssh_cfg())
        self.assert_pinned(self.pinned_keywords(["ssh", "-F", user_config]))

    @needs_tool("ssh")
    def test_herdrs_alias_keeps_loopback_verification_against_a_later_host_star(self):
        # herdr reaches the alias through ~/.ssh/config, where the operator's
        # own `Host *` may switch verification off for 127.0.0.1.
        self.write()
        user_config = os.path.join(self.home, ".ssh", "config")
        os.makedirs(os.path.dirname(user_config))
        with open(user_config, "w") as handle:
            handle.write(
                "Include %s\nHost *\n  NoHostAuthenticationForLocalhost yes\n"
                % yo.ssh_cfg()
            )
        resolved = self.effective(["ssh", "-F", user_config], yo.box.herdr_alias)
        self.assertEqual(resolved["nohostauthenticationforlocalhost"], "no")

    @needs_tool("ssh")
    def test_the_pin_is_looked_up_under_the_same_name_after_the_port_changes(self):
        # A port change (limactl edit --set .ssh.localPort) must not leave
        # ssh without a pin to check the new port's answer against.
        self.write()
        before = self.effective(["ssh", "-F", yo.ssh_cfg()])
        new_facts = yo.SshFacts(61999, *LIMA_FACTS[1:])
        with mock.patch.object(yo.LIMA, "ssh_facts", return_value=new_facts):
            self.write()
        after = self.effective(["ssh", "-F", yo.ssh_cfg()])
        self.assertEqual(before["port"], "60022")
        self.assertEqual(after["port"], "61999")
        self.assertEqual(after["hostkeyalias"], before["hostkeyalias"])
        self.assertEqual(after["hostkeyalias"], yo.box.vm_host)

    @needs_tool("ssh")
    def test_first_contact_overrides_all_three_pinned_keywords(self):
        self.write()
        effective = self.pinned_keywords(
            ["ssh"] + yo.ssh_options(yo.OPERATOR_FIRST_CONTACT)
        )
        self.assertEqual(effective["stricthostkeychecking"], "false")
        self.assertEqual(effective["userknownhostsfile"], "/dev/null")
        self.assertEqual(effective["nohostauthenticationforlocalhost"], "yes")


class TestKnownHostsLines(FakeHome):
    def test_formats_alias_type_and_key(self):
        pubkeys = "ssh-ed25519 AAAAC3abc== root@yolobox\n"
        lines = yo.known_hosts_lines(pubkeys)
        self.assertEqual(lines, ["lima-yolobox ssh-ed25519 AAAAC3abc=="])

    def test_a_line_names_no_host_and_no_port(self):
        # The pin must outlive the ssh port: a port in the line would tie it to one.
        lines = yo.known_hosts_lines("ssh-ed25519 AAAAC3abc== root@yolobox\n")
        self.assertNotIn("127.0.0.1", lines[0])
        self.assertNotIn("[", lines[0])

    def test_both_key_types_nixos_generates_pin_without_their_comment(self):
        pubkeys = (
            "ssh-ed25519 AAAAC3abc== root@yolobox\n"
            "ssh-rsa AAAAB3NzaC1yc2E= root@yolobox\n"
        )
        lines = yo.known_hosts_lines(pubkeys)
        self.assertEqual(
            lines,
            [
                "lima-yolobox ssh-ed25519 AAAAC3abc==",
                "lima-yolobox ssh-rsa AAAAB3NzaC1yc2E=",
            ],
        )

    def test_blank_lines_are_skipped(self):
        pubkeys = "\nssh-ed25519 AAAAC3abc== root@yolobox\n \t\n\n"
        self.assertEqual(
            yo.known_hosts_lines(pubkeys), ["lima-yolobox ssh-ed25519 AAAAC3abc=="]
        )

    def test_no_keys_make_no_lines(self):
        self.assertEqual(yo.known_hosts_lines(""), [])

    def test_a_line_that_is_not_a_host_key_fails_the_whole_fetch(self):
        # The fetch is the guest's words. A known_hosts marker, a second host
        # pattern, a comment or a key type the guest never generates must not
        # be written beside a real key, and must not be written on its own
        # either.
        key = "ssh-ed25519 AAAAC3abc== root@yolobox\n"
        for raw in (
            "@cert-authority * ssh-ed25519 AAAAC3abc==",
            "@revoked *",
            "lima-other ssh-ed25519 AAAAC3abc==",
            "ssh-ed25519 not-a-key",
            "ssh-ed25519",
            "not-a-key-line",
            "# host keys",
            "#ssh-ed25519 AAAAC3abc==",
            "ecdsa-sha2-nistp256 AAAAE2abc==",
            "ssh-dss AAAAB3NzaC1kc3M=",
            "ssh-ed25519\x1b[2J AAAAC3abc==",
        ):
            for pubkeys in (raw + "\n", key + raw + "\n", raw + "\n" + key):
                with self.subTest(pubkeys=pubkeys):
                    with self.assertRaisesRegex(yo.YoError, "not a host key"):
                        yo.known_hosts_lines(pubkeys)


class TestSshOptionValuesSurviveAHomeWithASpace(FakeHome):
    """Every -o value goes through ssh_opt(): ssh reads the argument as a
    config line, so an unquoted path with a blank is cut there and ssh exits
    255, which reads as a box that cannot be reached."""

    def effective(self, argv):
        completed = subprocess.run(
            list(argv) + ["-G", yo.box.vm_host],
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(line.split(" ", 1) for line in completed.stdout.splitlines())

    @needs_tool("ssh")
    def test_the_agents_control_path_and_the_forwarded_socket_reach_ssh_whole(self):
        home = os.path.join(self.home, "h o m e")
        os.makedirs(home)
        with with_home(home):
            os.makedirs(yo.instance_dir())
            with mock.patch.object(yo.LIMA, "ssh_facts", return_value=LIMA_FACTS):
                yo.sshconfig.write_ssh_config()
            argv = ["ssh"] + yo.ssh_options(yo.AGENT, mux=True)
            argv += yo.sshx.forward_agent_opts()
            effective = self.effective(argv)
            self.assertEqual(effective["controlpath"], yo.state.agent_control_path())
            self.assertEqual(effective["forwardagent"], yo.state.driver().op_sock())

    def test_a_value_with_a_double_quote_is_refused_not_mangled(self):
        for value in ('"', 'a"b', '"a b"'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(yo.YoError, "double quote"):
                    yo.ssh_opt("ControlPath", value)

    def test_a_plain_value_is_not_quoted_and_a_blank_one_is(self):
        self.assertEqual(yo.ssh_opt("User", "agent"), ["-o", "User=agent"])
        self.assertEqual(yo.ssh_opt("User", ""), ["-o", "User="])
        self.assertEqual(
            yo.ssh_opt("ControlPath", "/a b/c\td"), ["-o", 'ControlPath="/a b/c\td"']
        )


class TestEnsureGuestKnownHosts(FakeHome):
    def setUp(self):
        super().setUp()
        os.makedirs(yo.instance_dir())
        self.listener = mock.patch.object(yo.LIMA, "verify_vm_listener")
        self.listener.start()
        self.addCleanup(self.listener.stop)

    def test_fetches_via_operator_and_writes_the_known_hosts_file(self):
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()

        yo.LIMA.verify_vm_listener.assert_called_once_with(yo.SSH_LOCAL_PORT)
        role, argv = fake_ssh_run.call_args[0]
        # Nothing pinned yet: first contact is the only way to learn the key.
        self.assertIs(role, yo.OPERATOR_FIRST_CONTACT)
        self.assertEqual(argv[0], "sh")
        self.assertIn(yo.HOST_KEY_GLOB, argv[-1])

        with open(yo.known_hosts_path(), "r") as handle:
            content = handle.read()
        self.assertEqual(content, "lima-yolobox ssh-ed25519 AAAAC3abc==\n")
        self.assertEqual(stat.S_IMODE(os.stat(yo.known_hosts_path()).st_mode), 0o600)

    def write_pin(self, text):
        with open(yo.known_hosts_path(), "w") as handle:
            handle.write(text)

    def test_with_a_pin_the_fetch_is_pinned_and_a_passing_one_rewrites_it(self):
        self.write_pin("lima-yolobox ssh-ed25519 OLD==\n")
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        self.assertIs(fake_ssh_run.call_args[0][0], yo.OPERATOR)
        with open(yo.known_hosts_path()) as handle:
            self.assertEqual(handle.read(), "lima-yolobox ssh-ed25519 AAAAC3abc==\n")

    def test_a_pin_of_only_whitespace_is_no_pin(self):
        self.write_pin(" \n\t\n")
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        self.assertIs(fake_ssh_run.call_args[0][0], yo.OPERATOR_FIRST_CONTACT)

    def test_a_changed_key_is_sshs_refusal_and_the_pin_stays(self):
        # A VM recreated behind yo's back, or something else on the port: the
        # old pin is kept and the warning shown, never a key learned anew.
        self.write_pin("lima-yolobox ssh-ed25519 OLD==\n")
        warning = "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!"
        proc = mock.Mock(returncode=255, stdout="", stderr=warning + "\n")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertIn(warning, caught.exception.message)
        with open(yo.known_hosts_path()) as handle:
            self.assertEqual(handle.read(), "lima-yolobox ssh-ed25519 OLD==\n")

    def test_failed_fetch_raises_naming_the_cause(self):
        proc = mock.Mock(
            returncode=255,
            stdout="",
            stderr="ssh: connect to host 127.0.0.1 port 60022: Connection refused\n",
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertIn("Connection refused", caught.exception.message)
        # main() adds the "yo: " prefix itself.
        self.assertFalse(caught.exception.message.startswith("yo:"))

    def test_no_parseable_keys_raises(self):
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertFalse(caught.exception.message.startswith("yo:"))

    def test_the_fetch_dials_the_port_it_proved(self):
        # ssh.config may name another port (written before a port change, or
        # not at all yet); the fetch must reach the listener that was proved.
        facts = yo.SshFacts(60123, "op", "/k", "none")
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.LIMA, "ssh_facts", return_value=facts):
            with mock.patch.object(yo.proc, "run", return_value=proc) as fake_run:
                yo.ensure_guest_known_hosts()
        yo.LIMA.verify_vm_listener.assert_called_once_with(60123)
        argv = fake_run.call_args[0][0]
        self.assertEqual(argv[0], "ssh")
        pairs = list(zip(argv, argv[1:]))
        self.assertIn(("-o", "Port=60123"), pairs)
        # Before the host: ssh reads options only ahead of its destination.
        self.assertLess(pairs.index(("-o", "Port=60123")), argv.index(yo.box.vm_host))

    NO_HOST_KEY_FETCH = mock.Mock(
        returncode=0,
        stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n@revoked * ssh-rsa AAAAB3==\n",
        stderr="",
    )

    def test_a_first_fetch_with_a_line_that_is_no_host_key_pins_nothing(self):
        with mock.patch.object(yo.sshx, "ssh_run", return_value=self.NO_HOST_KEY_FETCH):
            with self.assertRaisesRegex(yo.YoError, "not a host key"):
                yo.ensure_guest_known_hosts()
        self.assertFalse(os.path.exists(yo.known_hosts_path()))

    def test_a_fetch_with_a_line_that_is_no_host_key_keeps_the_pin(self):
        self.write_pin("lima-yolobox ssh-ed25519 OLD==\n")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=self.NO_HOST_KEY_FETCH):
            with self.assertRaisesRegex(yo.YoError, "not a host key"):
                yo.ensure_guest_known_hosts()
        with open(yo.known_hosts_path()) as handle:
            self.assertEqual(handle.read(), "lima-yolobox ssh-ed25519 OLD==\n")

    def test_a_stranger_on_the_port_is_not_asked_and_pins_nothing(self):
        yo.LIMA.verify_vm_listener.side_effect = yo.YoError(
            "127.0.0.1:60022 is held by pid 77, so yo does not connect"
        )
        with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
            with self.assertRaisesRegex(yo.YoError, "does not connect"):
                yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()
        self.assertFalse(os.path.exists(yo.known_hosts_path()))


class Reached(Exception):
    pass


class TestDiskGrowSize(unittest.TestCase):
    def parse(self, arg):
        def fake_run(argv, **kwargs):
            raise Reached()

        with mock.patch.object(yo.proc, "run", fake_run):
            args = yo.build_parser().parse_args(["disk-grow", arg])
            yo.cmd_disk_grow(args)

    def parse_error(self, arg):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                self.parse(arg)
        self.assertEqual(caught.exception.code, 2)
        return stderr.getvalue()

    def test_whole_number_passes_validation(self):
        with self.assertRaises(Reached):
            self.parse("10")

    def test_rejects_what_is_not_a_whole_number_above_zero(self):
        # int() takes the last four; ٥ is the Arabic-Indic five.
        for value in ("1e3", "0", "-1", "٥", "+10", " 10 ", "1_0"):
            with self.subTest(value=value):
                self.assertIn(
                    "disk-grow size must be a whole number of GiB greater than 0, "
                    "got '%s'" % value,
                    self.parse_error(value),
                )

    def test_requires_exactly_one_argument(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["disk-grow"])
        self.assertEqual(caught.exception.code, 2)


class TestBoxSizeFlags(unittest.TestCase):
    """--memory and --disk of the commands that create a box."""

    def parse(self, *argv):
        return yo.build_parser().parse_args(list(argv))

    def parse_error(self, *argv):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                self.parse(*argv)
        self.assertEqual(caught.exception.code, 2)
        return stderr.getvalue()

    def test_the_smallest_box_and_a_larger_one_are_accepted(self):
        for command in ("up", "bootstrap"):
            with self.subTest(command=command):
                smallest = self.parse(
                    command,
                    "--memory",
                    str(yo.MIN_MEMORY_GIB),
                    "--disk",
                    str(yo.MIN_DISK_GIB),
                )
                self.assertEqual(
                    (smallest.memory, smallest.disk),
                    (yo.MIN_MEMORY_GIB, yo.MIN_DISK_GIB),
                )
                larger = self.parse(command, "--memory", "24", "--disk", "250")
                self.assertEqual((larger.memory, larger.disk), (24, 250))

    def test_no_flag_asks_for_nothing(self):
        args = self.parse("up")
        self.assertEqual((args.memory, args.disk, args.cpus), (None, None, None))

    def test_what_is_not_a_whole_number_of_gib_above_zero_is_refused(self):
        values = ("lizard", "24.5", "1e3", "", "NaN", "inf", "0", "-1")
        # int() takes all of these, and none is how a size is written (٤٠ is
        # 40 in Arabic-Indic digits).
        lookalikes = ("2_4", "+24", " 24 ", "٤٠")
        for flag in ("--memory", "--disk"):
            for value in values + lookalikes:
                with self.subTest(flag=flag, value=value):
                    self.assertIn(
                        "%s must be a whole number of GiB greater than 0, got '%s'"
                        % (flag, value),
                        self.parse_error("up", flag + "=" + value),
                    )

    def test_a_box_below_the_floor_is_refused_saying_why(self):
        under = yo.MIN_MEMORY_GIB - 1
        memory = self.parse_error("up", "--memory", str(under))
        self.assertIn(
            "--memory must be at least %d GiB, got %d" % (yo.MIN_MEMORY_GIB, under),
            memory,
        )
        self.assertIn("OOM-killed", memory)
        under = yo.MIN_DISK_GIB - 1
        disk = self.parse_error("bootstrap", "--disk", str(under))
        self.assertIn(
            "--disk must be at least %d GiB, got %d" % (yo.MIN_DISK_GIB, under), disk
        )
        self.assertIn("swapfile", disk)

    def test_an_abbreviated_flag_is_not_the_flag(self):
        self.assertIn(
            "unrecognized arguments: --mem", self.parse_error("up", "--mem", "24")
        )

    def test_only_the_commands_that_create_a_box_take_a_size(self):
        for flag in ("--memory", "--cpus"):
            with self.subTest(flag=flag):
                self.assertIn(
                    "unrecognized arguments", self.parse_error("down", flag, "2")
                )


class TestCpusFlag(unittest.TestCase):
    """--cpus is 1 to one less than the host's CPU count, so the host keeps one."""

    def parse(self, value, host):
        with mock.patch.object(os, "cpu_count", return_value=host):
            return yo.build_parser().parse_args(["up", "--cpus=" + value])

    def error(self, value, host):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                self.parse(value, host)
        self.assertEqual(caught.exception.code, 2)
        return stderr.getvalue()

    def test_one_and_all_but_one_of_the_hosts_cpus_are_accepted(self):
        self.assertEqual(self.parse("1", 8).cpus, 1)
        self.assertEqual(self.parse("7", 8).cpus, 7)
        self.assertEqual(self.parse("1", 2).cpus, 1)

    def test_every_other_value_is_refused_naming_the_range_and_the_hosts_count(self):
        # ٢ is 2 in Arabic-Indic digits.
        for value in ("0", "-1", "8", "9", "lizard", "", "2.5", "+2", " 2 ", "٢"):
            with self.subTest(value=value):
                self.assertIn(
                    "--cpus must be a whole number of CPUs from 1 to 7 (this host "
                    "has 8 CPUs and keeps at least 1), got '%s'" % value,
                    self.error(value, 8),
                )

    def test_a_host_with_one_cpu_has_no_range_and_says_so(self):
        for value in ("1", "0", "2"):
            with self.subTest(value=value):
                self.assertIn("this host has only 1 CPU", self.error(value, 1))

    def test_an_unknown_host_count_refuses_instead_of_leaving_the_range_open(self):
        self.assertIn("cannot read the host's CPU count", self.error("2", None))

    def test_bootstrap_takes_it_too(self):
        with mock.patch.object(os, "cpu_count", return_value=4):
            args = yo.build_parser().parse_args(["bootstrap", "--cpus", "3"])
        self.assertEqual(args.cpus, 3)


AWS_CONFIG_TEXT = """[default]
sso_start_url = https://example.awsapps.com/start
region = eu-west-1

[profile work]
sso_session = my-sso
region = us-east-1

[profile legacy]
aws_access_key_id = AKIA...

[profile noregion]
sso_start_url = https://example.awsapps.com/start

[sso-session my-sso]
sso_start_url = https://example.awsapps.com/start
sso_region = us-east-1
"""


class TestAwsAllowlist(FakeHome):
    def write_aws_config(self, text):
        aws_dir = os.path.join(self.home, ".aws")
        os.makedirs(aws_dir, exist_ok=True)
        with open(os.path.join(aws_dir, "config"), "w") as handle:
            handle.write(text)

    def test_no_config_file_is_an_empty_allowlist(self):
        self.assertEqual(yo.aws_allowlist(), [])

    def test_default_and_sso_session_profiles_are_included(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo.ui, "err"):
            allowed = yo.aws_allowlist()
        self.assertIn(("default", "eu-west-1"), allowed)
        self.assertIn(("work", "us-east-1"), allowed)

    def test_sso_session_section_itself_is_excluded(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo.ui, "err"):
            allowed = yo.aws_allowlist()
        self.assertNotIn("my-sso", [name for name, _ in allowed])

    def test_profile_without_sso_fields_is_excluded(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo.ui, "err"):
            allowed = yo.aws_allowlist()
        self.assertNotIn("legacy", [name for name, _ in allowed])

    def test_profile_without_region_is_excluded_and_warned(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo.ui, "err") as fake_err:
            allowed = yo.aws_allowlist()
        self.assertNotIn("noregion", [name for name, _ in allowed])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("noregion", messages)

    def test_default_spelled_twice_is_listed_once_from_the_last_section_with_a_warning(
        self,
    ):
        # botocore's build_profile_map lets the later section win, and so does
        # the broker's `aws configure export-credentials`; the list must equal
        # the broker's own (a dict keyed by name) or yo would restart it on
        # every run.
        sso = "sso_start_url = https://example.awsapps.com/start\n"
        self.write_aws_config(
            "[default]\n%sregion = eu-west-1\n"
            "[profile default]\n%sregion = us-east-1\n" % (sso, sso)
        )
        with mock.patch.object(yo.ui, "err") as fake_err:
            allowed = yo.aws_allowlist()
        self.assertEqual(allowed, [("default", "us-east-1")])
        fake_err.assert_called_once()
        self.assertIn("[default] and [profile default]", fake_err.call_args.args[0])
        self.assertIn("using [profile default]", fake_err.call_args.args[0])

    def test_the_last_default_section_decides_whether_the_profile_is_sso(self):
        self.write_aws_config(
            "[profile default]\nsso_start_url = https://example.awsapps.com/start\n"
            "region = eu-west-1\n[default]\nregion = us-east-1\n"
        )
        with mock.patch.object(yo.ui, "err"):
            self.assertEqual(yo.aws_allowlist(), [])

    def test_unparseable_config_raises_yoerror_naming_the_path(self):
        config_path = os.path.join(self.home, ".aws", "config")
        self.write_aws_config("[default]\n[default]\nregion = us-east-1\n")
        with self.assertRaises(yo.YoError) as caught:
            yo.aws_allowlist()
        self.assertIn(config_path, caught.exception.message)

    def test_unreadable_encoding_raises_yoerror_naming_the_path(self):
        config_path = os.path.join(self.home, ".aws", "config")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "wb") as handle:
            handle.write(b"[profile work]\nregion = \xff\xfe\nsso_start_url = x\n")
        with self.assertRaises(yo.YoError) as caught:
            yo.aws_allowlist()
        self.assertIn(config_path, caught.exception.message)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 file")
    def test_a_config_that_cannot_be_opened_raises_yoerror_naming_the_path(self):
        config_path = os.path.join(self.home, ".aws", "config")
        self.write_aws_config(AWS_CONFIG_TEXT)
        os.chmod(config_path, 0)
        with self.assertRaises(yo.YoError) as caught:
            yo.aws_allowlist()
        self.assertIn(config_path, caught.exception.message)
        self.assertIn("Permission denied", caught.exception.message)

    def test_profile_name_with_shell_metacharacters_is_excluded_and_warned(self):
        self.write_aws_config(
            "[profile work; rm -rf /]\n"
            "sso_start_url = https://example.awsapps.com/start\n"
            "region = us-east-1\n"
        )
        with mock.patch.object(yo.ui, "err") as fake_err:
            allowed = yo.aws_allowlist()
        self.assertEqual(allowed, [])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("work; rm -rf /", messages)

    def allowed_for(self, section, region="us-east-1"):
        self.write_aws_config(
            "[%s]\nsso_start_url = https://example.awsapps.com/start\nregion = %s\n"
            % (section, region)
        )
        with mock.patch.object(yo.ui, "err") as fake_err:
            allowed = yo.aws_allowlist()
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        return allowed, messages

    def test_profile_names_that_could_be_options_or_paths_are_excluded(self):
        for name in ["-rf", "--profile", ".hidden", "_x", "a b", "a/b", "é"]:
            with self.subTest(name=name):
                allowed, messages = self.allowed_for("profile " + name)
                self.assertEqual(allowed, [])
                self.assertIn(name, messages)

    def test_ordinary_profile_names_are_kept(self):
        for name in ["work", "a.b-c_d", "0prod", "Prod-2"]:
            with self.subTest(name=name):
                allowed, _ = self.allowed_for("profile " + name)
                self.assertEqual(allowed, [(name, "us-east-1")])

    def test_real_region_names_are_kept(self):
        regions = [
            "us-east-1",
            "ap-southeast-2",
            "us-gov-west-1",
            "il-central-1",
            "eusc-de-east-1",
        ]
        for region in regions:
            with self.subTest(region=region):
                allowed, _ = self.allowed_for("profile work", region)
                self.assertEqual(allowed, [("work", region)])

    def test_malformed_regions_exclude_the_profile_with_a_warning(self):
        regions = [
            "US-EAST-1",
            "us-east",
            "us-east-1\n  continued",
            "us-east-1; rm -rf /",
            "us-east-\u0661",
            "-east-1",
            "us--1",
            "u-east-1",
            "abcde-east-1",
        ]
        for region in regions:
            with self.subTest(region=region):
                allowed, messages = self.allowed_for("profile work", region)
                self.assertEqual(allowed, [])
                self.assertIn("work", messages)
                self.assertIn("not an AWS region name", messages)


class BrokerStub:
    """A server on the broker's socket path that answers /health as told."""

    def __init__(self, path, status, body):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(status)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def address_string(handler):
                return "unix"

            def log_message(handler, *args):
                pass

        class Server(socketserver.UnixStreamServer):
            pass

        self.path = path
        self.server = Server(path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        os.unlink(self.path)


class TestAwsBrokerHealth(FakeHome):
    def health_on(self, sock, status, body):
        os.makedirs(yo.state.aws_broker_dir(), exist_ok=True)
        stub = BrokerStub(sock, status, body)
        try:
            return yo.aws_broker_health()
        finally:
            stub.close()

    def health_of(self, status, body):
        return self.health_on(yo.state.aws_broker_ctl_sock(), status, body)

    def test_a_healthy_broker_carries_what_it_reported(self):
        info = {"pid": 5, "watch_pid": 7, "allow": ["work"]}
        health = self.health_of(200, json.dumps(info).encode())
        self.assertEqual(health, info)

    def test_any_other_answer_is_an_error_showing_it(self):
        # A broker that answers wrong still runs and holds its socket.
        for status, body in (
            (500, b"oops"),
            (503, b"broker busy; retry\n"),
            (404, b"{}"),
            (200, b"not json"),
            (200, b"[1, 2]"),
            (200, b"lizard"),
            (200, b""),
        ):
            with self.subTest(status=status, body=body):
                with self.assertRaises(yo.YoError) as caught:
                    self.health_of(status, body)
                self.assertIn(
                    "answered /health with HTTP %d and %r" % (status, body),
                    caught.exception.message,
                )

    def test_no_socket_means_no_broker_listens(self):
        self.assertIsNone(yo.aws_broker_health())

    def test_a_socket_nothing_accepts_on_means_no_broker_listens(self):
        os.makedirs(yo.state.aws_broker_dir(), exist_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(yo.state.aws_broker_ctl_sock())
        stale.close()
        self.assertIsNone(yo.aws_broker_health())

    def test_a_broker_that_never_answers_is_an_error_naming_it(self):
        os.makedirs(yo.state.aws_broker_dir(), exist_ok=True)
        mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(mute.close)
        mute.bind(yo.state.aws_broker_ctl_sock())
        mute.listen(1)
        with self.assertRaisesRegex(yo.YoError, "cannot ask the aws-broker on"):
            yo.aws.health_on(yo.state.aws_broker_ctl_sock(), 0.2)

    def test_a_broker_without_a_usable_pid_is_named_by_its_socket(self):
        sock = yo.state.aws_broker_ctl_sock()
        health = self.health_on(sock, 200, json.dumps({"pid": None}).encode())
        with self.assertRaises(yo.YoError) as caught:
            yo.kill_broker_in(health)
        self.assertIn(
            "the aws-broker on %s answered /health" % sock, caught.exception.message
        )

    def test_the_forwarded_socket_is_never_asked(self):
        # The guest can crowd out the forwarded socket, so a broker that answers
        # only there is as good as none.
        info = {"pid": 5, "watch_pid": 7, "allow": ["work"]}
        health = self.health_on(yo.aws_broker_sock(), 200, json.dumps(info).encode())
        self.assertIsNone(health)


class TestOpProbeAlive(unittest.TestCase):
    def proc(self, returncode, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_exit_0_is_alive(self):
        self.assertTrue(
            yo.op_probe_alive(self.proc(0, "2048 SHA256:... user@host (RSA)\n"))
        )

    def test_exit_1_no_identities_is_alive(self):
        self.assertTrue(
            yo.op_probe_alive(self.proc(1, "The agent has no identities.\n"))
        )

    def test_exit_1_communication_failure_is_dead(self):
        self.assertFalse(
            yo.op_probe_alive(
                self.proc(
                    1, "error fetching identities: communication with agent failed\n"
                )
            )
        )

    def test_exit_2_is_dead(self):
        self.assertFalse(
            yo.op_probe_alive(
                self.proc(
                    2, "Could not open a connection to your authentication agent.\n"
                )
            )
        )


BAD_PIDS = [0, 1, -1, True, False, "123", 12.0, None]


class TestCheckedPid(unittest.TestCase):
    def test_a_plain_pid_above_init_is_accepted(self):
        self.assertEqual(yo.checked_pid(2), 2)

    def test_anything_else_is_refused(self):
        for value in BAD_PIDS:
            with self.subTest(value=value):
                self.assertIsNone(yo.checked_pid(value))


class TestBrokerNeedsRestart(unittest.TestCase):
    def test_a_watch_pid_that_is_not_a_real_pid_needs_restart(self):
        for value in BAD_PIDS:
            with self.subTest(value=value):
                health = {"pid": 5, "watch_pid": value, "allow": ["work"]}
                self.assertTrue(yo.broker_needs_restart(health, 123, ["work"]))

    def test_an_unreachable_broker_needs_restart(self):
        self.assertTrue(yo.broker_needs_restart(None, 123, ["work"]))

    def test_matching_watch_pid_and_allowlist_does_not_restart(self):
        health = {"pid": 1, "watch_pid": 123, "allow": ["work"]}
        self.assertFalse(yo.broker_needs_restart(health, 123, ["work"]))

    def test_stale_watch_pid_needs_restart(self):
        health = {"pid": 1, "watch_pid": 999, "allow": ["work"]}
        self.assertTrue(yo.broker_needs_restart(health, 123, ["work"]))

    def test_allowlist_drift_needs_restart(self):
        health = {"pid": 1, "watch_pid": 123, "allow": ["work"]}
        self.assertTrue(yo.broker_needs_restart(health, 123, ["personal", "work"]))


class TestEnsureAwsBrokerStaleBroker(FakeHome):
    def test_kills_the_brokers_own_pid_not_the_watched_pid(self):
        health = {"pid": 111, "watch_pid": 999, "allow": ["work"]}
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(yo.box.driver, "keepalive_pid", return_value=555),
            mock.patch.object(yo.aws, "aws_broker_health", return_value=health),
            mock.patch.object(yo.aws, "stop_broker") as fake_stop,
            mock.patch.object(yo.aws, "start_aws_broker") as fake_start,
            mock.patch.object(yo.aws, "push_aws_guest_files"),
        ):
            result = yo.ensure_aws_broker()
        self.assertTrue(result)
        fake_stop.assert_called_once_with(111)
        fake_start.assert_called_once_with(555, ["work"])


@unittest.skipUnless(
    hasattr(os, "pidfd_open") or hasattr(select, "kqueue"),
    "needs os.pidfd_open or select.kqueue to watch a pid",
)
class TestStopBroker(unittest.TestCase):
    """stop_broker against real children: one that exits on SIGTERM, one that
    ignores it, one already gone."""

    def child(self, body):
        """A child that has run `body` (a signal disposition is in place
        before any signal is sent), then waits for its stdin to close."""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                body + "\nimport sys; print('ready', flush=True); sys.stdin.read()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline(), b"ready\n")
        return child

    def test_a_broker_that_exits_on_sigterm_is_stopped(self):
        child = self.child("")
        yo.aws.stop_broker(child.pid)
        self.assertEqual(child.wait(30), -signal.SIGTERM)

    def test_a_broker_that_ignores_sigterm_is_an_error_naming_it(self):
        child = self.child(
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)"
        )
        with mock.patch.object(yo.aws, "STALE_BROKER_EXIT_SECONDS", 0):
            with self.assertRaisesRegex(
                yo.YoError, "stale aws-broker \\(pid %d\\) did not exit" % child.pid
            ):
                yo.aws.stop_broker(child.pid)
        self.assertIsNone(child.poll())

    def test_a_pid_that_is_gone_is_stopped_already(self):
        child = self.child("")
        child.stdin.close()
        child.wait()
        yo.aws.stop_broker(child.pid)


class TestStaleBrokerPidIsValidated(FakeHome):
    def test_a_pid_that_is_not_a_real_pid_is_never_signalled_and_is_an_error(self):
        # A broker that answers but cannot be stopped is reported, not skipped.
        for value in BAD_PIDS:
            with self.subTest(value=value):
                health = {"pid": value, "watch_pid": 999, "allow": []}
                with mock.patch.object(yo.os, "kill") as fake_kill:
                    with self.assertRaisesRegex(yo.YoError, "cannot stop it"):
                        yo.kill_broker_in(health)
                fake_kill.assert_not_called()

    def test_an_unreachable_broker_has_nothing_to_stop(self):
        with mock.patch.object(yo.os, "kill") as fake_kill:
            yo.kill_broker_in(None)
        fake_kill.assert_not_called()


class TestStartAwsBrokerFiles(FakeHome):
    def start(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 0
        with (
            mock.patch.object(yo.subprocess, "Popen", return_value=proc),
            mock.patch.object(
                yo.aws,
                "read_handshake",
                return_value=yo.aws.Handshake(["READY"], False),
            ),
        ):
            yo.start_aws_broker(555, ["work"])

    def test_the_broker_is_told_both_its_sockets(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 0
        with (
            mock.patch.object(yo.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(
                yo.aws,
                "read_handshake",
                return_value=yo.aws.Handshake(["READY"], False),
            ),
        ):
            yo.start_aws_broker(555, ["work"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--socket") + 1], yo.aws_broker_sock())
        self.assertEqual(
            argv[argv.index("--control-socket") + 1], yo.state.aws_broker_ctl_sock()
        )
        self.assertNotEqual(yo.aws_broker_sock(), yo.state.aws_broker_ctl_sock())

    def test_the_broker_does_not_inherit_yos_stdin(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 0
        with (
            mock.patch.object(yo.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(
                yo.aws,
                "read_handshake",
                return_value=yo.aws.Handshake(["READY"], False),
            ),
        ):
            yo.start_aws_broker(555, ["work"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_a_broker_program_that_is_missing_is_a_yo_error_not_a_traceback(self):
        with mock.patch.object(yo.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(yo.YoError, "aws-broker not found on PATH"):
                yo.start_aws_broker(555, ["work"])

    def test_the_broker_dir_and_log_are_private(self):
        self.start()
        log_dir = yo.state.aws_broker_dir()
        self.assertEqual(stat.S_IMODE(os.stat(log_dir).st_mode), 0o700)
        log = os.path.join(log_dir, "broker.log")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_an_existing_permissive_dir_is_tightened(self):
        log_dir = yo.state.aws_broker_dir()
        os.makedirs(log_dir)
        os.chmod(log_dir, 0o755)
        self.start()
        self.assertEqual(stat.S_IMODE(os.stat(log_dir).st_mode), 0o700)

    def test_an_existing_log_is_truncated_not_appended_to(self):
        log_dir = yo.state.aws_broker_dir()
        os.makedirs(log_dir)
        log = os.path.join(log_dir, "broker.log")
        with open(log, "w") as handle:
            handle.write("old secrets")
        self.start()
        self.assertEqual(os.path.getsize(log), 0)


class TestStartAwsBrokerHandshakeFailures(FakeHome):
    """start_aws_broker against real children: a script standing in for aws-broker."""

    def start_with_broker_script(self, body):
        script_dir, _ = fake_bin(self, {"aws-broker": "#!/bin/sh\n" + body})
        script = os.path.join(script_dir, "aws-broker")
        spawn = yo.proc.spawn
        with mock.patch.object(
            yo.proc,
            "spawn",
            lambda argv, **kwargs: spawn([script, *argv[1:]], **kwargs),
        ):
            yo.start_aws_broker(555, ["work"])

    def test_a_broker_that_exits_before_its_handshake_is_reported_with_its_exit_code(
        self,
    ):
        with self.assertRaises(yo.YoError) as caught:
            self.start_with_broker_script("echo boom >&2\nexit 3\n")
        message = caught.exception.message
        self.assertIn("exited with code 3", message)
        self.assertIn("broker.log", message)
        self.assertNotIn("no handshake", message)

    def test_a_broker_killed_by_a_signal_is_named_by_the_signal(self):
        with self.assertRaises(yo.YoError) as caught:
            self.start_with_broker_script("kill -KILL $$\n")
        self.assertIn(
            "killed by SIGKILL before its handshake", caught.exception.message
        )
        self.assertNotIn("-9", caught.exception.message)

    def test_a_broker_that_closes_its_output_but_runs_on_is_not_waited_for(self):
        spawned = []
        spawn = yo.proc.spawn

        def spawned_once_its_output_closed(argv, **kwargs):
            # select does not consume EOF: yo then finds the pipe closed, so a zero
            # handshake time decides nothing but how long yo waits for the exit.
            spawned.append(spawn(argv, **kwargs))
            select.select([spawned[-1].stdout], [], [])
            return spawned[-1]

        with (
            mock.patch.object(yo.aws, "BROKER_HANDSHAKE_SECONDS", 0),
            mock.patch.object(yo.proc, "spawn", spawned_once_its_output_closed),
        ):
            with self.assertRaisesRegex(
                yo.YoError, "closed its output without exiting"
            ):
                self.start_with_broker_script("exec 1>&-\nexec sleep 60\n")
        self.assertIsNotNone(spawned[0].poll())

    def test_a_broker_that_stays_silent_is_reported_as_no_handshake(self):
        # The broker never writes or closes its output, so no deadline makes it speak.
        with mock.patch.object(yo.aws, "BROKER_HANDSHAKE_SECONDS", 0):
            with self.assertRaisesRegex(yo.YoError, "no handshake within"):
                self.start_with_broker_script("exec sleep 60\n")

    def test_a_broker_that_reports_an_error_is_reported_with_it(self):
        with self.assertRaisesRegex(yo.YoError, "aws-broker failed: no good"):
            self.start_with_broker_script("echo ERROR=no good\nexit 1\n")


class TestEnsureAwsBrokerDisabling(FakeHome):
    def test_no_aws_cli_stops_a_running_broker_and_clears_the_guest_config(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo.aws, "disable_aws_broker") as fake_disable,
        ):
            result = yo.ensure_aws_broker()
        self.assertFalse(result)
        fake_disable.assert_called_once_with()

    def test_no_sso_profiles_stops_a_running_broker_and_clears_the_guest_config(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(yo.aws, "aws_allowlist", return_value=[]),
            mock.patch.object(yo.aws, "disable_aws_broker") as fake_disable,
        ):
            result = yo.ensure_aws_broker()
        self.assertFalse(result)
        fake_disable.assert_called_once_with()

    def test_disable_kills_the_brokers_pid_and_removes_the_guest_config(self):
        killed = []
        removed = []

        def fake_ssh_run(role, argv, **kwargs):
            removed.append(list(argv))
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(
                yo.aws,
                "aws_broker_health",
                return_value={"pid": 222, "watch_pid": 1, "allow": []},
            ),
            mock.patch.object(
                yo.aws, "stop_broker", side_effect=lambda pid: killed.append(pid)
            ),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
        ):
            yo.disable_aws_broker()
        self.assertEqual(killed, [222])
        self.assertEqual(removed, [["rm", "-f", yo.GUEST_AWS_CONFIG_PATH]])


class TestDisableAwsBrokerFailsLoudly(FakeHome):
    def disable_with_ssh_exit(self, returncode, stderr):
        with (
            mock.patch.object(yo.aws, "aws_broker_health", return_value=None),
            mock.patch.object(
                yo.proc,
                "run_capped",
                return_value=yo.proc.GuestRun([], returncode, b"", stderr),
            ),
        ):
            yo.disable_aws_broker()

    def test_a_guest_config_that_cannot_be_removed_is_an_error_not_a_note(self):
        with self.assertRaisesRegex(
            yo.YoError, r"rm -f .*aws-config.*\n  \| Permission denied"
        ):
            self.disable_with_ssh_exit(1, b"Permission denied")

    def test_a_removed_guest_config_returns_quietly(self):
        with mock.patch.object(yo.ui, "err") as fake_err:
            self.disable_with_ssh_exit(0, b"")
        fake_err.assert_not_called()


class TestGuestAwsConfigText(unittest.TestCase):
    def test_renders_one_profile_section_per_entry(self):
        text = yo.guest_aws_config_text([("work", "us-east-1")])
        self.assertEqual(
            text,
            "[profile work]\n"
            "region = us-east-1\n"
            "credential_process = yolobox-guest aws-creds work\n",
        )

    def test_renders_every_entry_in_order(self):
        text = yo.guest_aws_config_text([("a", "r1"), ("b", "r2")])
        self.assertLess(text.index("[profile a]"), text.index("[profile b]"))


MISSING = object()


def df_output(
    kib_line="/dev/vda2 1000 500 500 50% /", human_line="/dev/vda2 59G 10G 46G 18% /"
):
    """What the guest prints for yo.guest.ROOT_USAGE_PROBE: `df -h /` then `df -kP /`."""
    return (
        "Filesystem Size Used Avail Use% Mounted on\n"
        + human_line
        + "\nFilesystem 1024-blocks Used Available Capacity Mounted on\n"
        + kib_line
        + "\n"
    )


def fake_limactl(
    stdout, returncode=0, stderr="", listed=None, listing_returncode=0, module=None
):
    """Patch proc.run to play limactl: `list -q` names `listed` (default: the
    box itself), and `list NAME --json` answers stdout, returncode and stderr.
    `module` is the loaded yo copy to patch, default the shared one."""
    target = module or yo

    def run(argv, **kwargs):
        if list(argv) == ["limactl", "--log-level=warn", "list", "-q"]:
            names = [target.box.name] if listed is None else listed
            failure = "limactl: list exploded" if listing_returncode else ""
            return mock.Mock(
                returncode=listing_returncode,
                stdout="".join(n + "\n" for n in names),
                stderr=failure,
            )
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    return mock.patch.object(target.proc, "run", side_effect=run)


class TestDeletingPaths(FakeHome):
    def test_a_path_that_is_already_gone_is_the_wanted_end_state(self):
        yo.proc.delete_tree(os.path.join(self.home, "gone"))

    def test_a_path_of_the_wrong_kind_is_a_yo_error_naming_it(self):
        file = os.path.join(self.home, "file")
        Path(file).touch()
        with self.assertRaisesRegex(yo.YoError, "cannot delete .*file: "):
            yo.proc.delete_tree(file)

    def test_a_symlink_is_refused_with_the_reason_not_with_none(self):
        link = os.path.join(self.home, "link")
        os.symlink(self.home, link)
        with self.assertRaises(yo.YoError) as caught:
            yo.proc.delete_tree(link)
        self.assertIn("symbolic link", caught.exception.message)
        self.assertNotIn("None", caught.exception.message)
        self.assertTrue(os.path.islink(link))

    @unittest.skipIf(os.geteuid() == 0, "root can delete inside a read-only directory")
    def test_a_failure_inside_a_tree_names_the_file_that_failed(self):
        tree = os.path.join(self.home, "tree")
        locked = os.path.join(tree, "locked")
        os.makedirs(locked)
        Path(locked, "stuck").touch()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        with self.assertRaisesRegex(
            yo.YoError, "cannot delete .*tree: Permission denied at .*tree/locked"
        ):
            yo.proc.delete_tree(tree)


class TestOsErrorWithoutErrno(FakeHome):
    def test_a_message_spells_an_error_with_no_errno_by_its_own_text(self):
        # strerror is None for OSError("lizard"); the message must not print it.
        with mock.patch("builtins.open", side_effect=OSError("lizard")):
            with self.assertRaises(yo.YoError) as caught:
                yo.sshconfig.add_ssh_config_include()
        self.assertIn(": lizard", caught.exception.message)
        self.assertNotIn("None", caught.exception.message)


class TestLimaDestroy(FakeHome):
    """`yo destroy` on Lima: one `limactl delete --force`, for an instance that exists."""

    def setUp(self):
        super().setUp()
        self.listed = [yo.box.name]
        self.list_code = 0
        self.agent_master = False
        self.agent_master_survives_exit = False
        self.delete_code = 0
        self.calls = []
        self.lines = []
        for patcher in (
            mock.patch.object(yo.subprocess, "run", side_effect=self.subprocess_run),
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo.ui, "out", side_effect=self.lines.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def subprocess_run(self, argv, **kwargs):
        self.calls.append(list(argv))
        code, stdout, stderr = 0, b"", b""
        if argv[:4] == ["limactl", "--log-level=warn", "list", "-q"]:
            code = self.list_code
            stdout = "".join(name + "\n" for name in self.listed).encode()
        elif argv[:3] == ["limactl", "--log-level=warn", "delete"]:
            code = self.delete_code
        elif argv[0] == "ssh" and "check" in argv:
            code, stderr = (
                (0, b"Master running (pid=4242)\r\n")
                if self.agent_master
                else (255, b"Control socket connect(x): No such file or directory\r\n")
            )
        elif argv[0] == "ssh" and "exit" in argv:
            self.agent_master = self.agent_master_survives_exit
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def destroy(self, yes):
        yo.cmd_destroy(argparse.Namespace(yes=yes))

    def deletes(self):
        return [
            call
            for call in self.calls
            if call[:3] == ["limactl", "--log-level=warn", "delete"]
        ]

    def test_yes_force_deletes_the_listed_instance(self):
        self.destroy(yes=True)
        self.assertEqual(
            self.deletes(),
            [["limactl", "--log-level=warn", "delete", "--force", "yolobox"]],
        )

    def test_a_failing_delete_is_a_yo_error(self):
        self.delete_code = 1
        with self.assertRaisesRegex(
            yo.YoError, "limactl --log-level=warn delete --force yolobox failed"
        ):
            self.destroy(yes=True)

    def test_the_agents_ssh_master_quits_before_its_socket_directory_is_deleted(self):
        self.agent_master = True
        self.destroy(yes=False)
        self.assertEqual(self.deletes(), [])
        self.assertIn("close the agent's ssh master", "\n".join(self.lines))
        self.destroy(yes=True)
        self.assertFalse(self.agent_master)
        exit_at = next(i for i, call in enumerate(self.calls) if "exit" in call)
        self.assertLess(exit_at, self.calls.index(self.deletes()[0]))

    def test_the_boxs_broker_stops_before_its_directory_is_deleted(self):
        # A broker that ends with the hostagent would unlink its sockets in
        # the directory limactl is deleting.
        stopped = []

        def stop(pid):
            stopped.append(pid)
            self.calls.append(["stop the broker", pid])

        with (
            mock.patch.object(yo.aws, "aws_broker_health", return_value={"pid": 77}),
            mock.patch.object(yo.aws, "stop_broker", side_effect=stop),
        ):
            self.destroy(yes=False)
            self.assertIn("stop the AWS broker of yolobox", "\n".join(self.lines))
            self.assertEqual(stopped, [])
            self.destroy(yes=True)
        self.assertEqual(stopped, [77])
        self.assertLess(
            self.calls.index(["stop the broker", 77]),
            self.calls.index(self.deletes()[0]),
        )

    def test_an_agent_master_that_will_not_quit_stops_the_run(self):
        self.agent_master = True
        self.agent_master_survives_exit = True
        with self.assertRaisesRegex(yo.YoError, "agent ssh master is still running"):
            self.destroy(yes=True)
        self.assertEqual(self.deletes(), [])

    def test_without_yes_nothing_is_deleted(self):
        self.destroy(yes=False)
        self.assertEqual(self.deletes(), [])
        self.assertIn("delete the Lima instance yolobox", "\n".join(self.lines))

    def test_an_instance_that_is_not_listed_has_nothing_to_destroy(self):
        self.listed = ["my-yolobox"]
        for yes in (False, True):
            with self.subTest(yes=yes):
                self.lines.clear()
                self.destroy(yes=yes)
                self.assertEqual(self.deletes(), [])
                self.assertEqual(self.lines, ["yolobox has nothing to destroy"])

    def test_a_limactl_that_cannot_list_is_not_a_box_with_nothing_to_destroy(self):
        self.list_code = 1
        with self.assertRaisesRegex(yo.YoError, "list.*failed"):
            self.destroy(yes=True)


class TestLimaConfigGaps(FakeHome):
    def mock_lima_list(
        self,
        forwards,
        host_agent_pid=123,
        mounts=MISSING,
        forward_agent=None,
        over_vsock=False,
        propagate_proxy_env=False,
        load_dot_ssh_pub_keys=False,
        user_uid=501,
        vm_type="vz",
    ):
        config = {"portForwards": forwards, "vmType": vm_type}
        if mounts is not MISSING:
            config["mounts"] = mounts
        config["ssh"] = {}
        if forward_agent is not None:
            config["ssh"]["forwardAgent"] = forward_agent
        if over_vsock is not None:
            config["ssh"]["overVsock"] = over_vsock
        if propagate_proxy_env is not MISSING:
            config["propagateProxyEnv"] = propagate_proxy_env
        if load_dot_ssh_pub_keys is not MISSING:
            config["ssh"]["loadDotSSHPubKeys"] = load_dot_ssh_pub_keys
        if user_uid is not MISSING:
            config["user"] = {"uid": user_uid}
        payload = json.dumps({"hostAgentPID": host_agent_pid, "config": config})
        return fake_limactl(payload)

    def test_missing_instance_is_no_gaps(self):
        with fake_limactl("", 1, "whatever limactl says", listed=["other"]):
            self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_an_instance_is_missing_only_when_its_exact_name_is_not_listed(self):
        with fake_limactl("", 1, "vz exploded", listed=["yolobox2", "my-yolobox"]):
            self.assertIsNone(yo.lima.lima_instance_info())

    def test_a_limactl_that_fails_is_not_a_box_without_gaps(self):
        with fake_limactl("", 1, "vz exploded"):
            with self.assertRaisesRegex(
                yo.YoError,
                "limactl --log-level=warn list yolobox --json failed .exit 1.: vz exploded",
            ):
                yo.LIMA.config_gaps()

    def test_a_listing_that_fails_is_not_a_missing_instance(self):
        with fake_limactl("", listing_returncode=1):
            with self.assertRaisesRegex(
                yo.YoError,
                "limactl --log-level=warn list -q failed .exit 1.: limactl: list exploded",
            ):
                yo.LIMA.config_gaps()

    def test_an_answer_that_is_not_a_json_object_is_not_a_box_without_gaps(self):
        for stdout in ("garbage", "[]", ""):
            with self.subTest(stdout=stdout):
                with fake_limactl(stdout):
                    with self.assertRaisesRegex(
                        yo.YoError, "did not print one JSON object"
                    ):
                        yo.LIMA.config_gaps()

    def test_a_listing_without_a_config_object_is_an_error_not_a_box_without_gaps(
        self,
    ):
        # A null or scalar config would read as every setting unset, which is
        # every gap at once, or as none: both wrong for a listing yo cannot read.
        for config, said in (
            (None, "printed config as None, not a JSON object"),
            ("lizard", "printed config as 'lizard', not a JSON object"),
            ([], "printed config as [], not a JSON object"),
            (MISSING, "printed no config for yolobox"),
        ):
            with self.subTest(config=config):
                data = {"hostAgentPID": 5, "status": "Stopped"}
                if config is not MISSING:
                    data["config"] = config
                with fake_limactl(json.dumps(data)):
                    for read in (
                        yo.LIMA.config_gaps,
                        yo.LIMA.vm_status,
                        yo.LIMA.keepalive_pid,
                    ):
                        with self.assertRaises(yo.YoError) as caught:
                            read()
                        self.assertIn(
                            "limactl --log-level=warn list yolobox --json",
                            caught.exception.message,
                        )
                        self.assertIn(said, caught.exception.message)

    def test_an_ssh_or_user_that_is_not_an_object_is_an_error(self):
        for config in (
            {"ssh": None},
            {"ssh": 1},
            {"ssh": {}, "user": None},
            {"ssh": {}, "user": "lizard"},
        ):
            with self.subTest(config=config):
                with fake_limactl(json.dumps({"config": config})):
                    with self.assertRaisesRegex(yo.YoError, "not a JSON object"):
                        yo.LIMA.config_gaps()

    def test_a_broken_instance_is_an_error_quoting_what_lima_said(self):
        # Lima refuses to start or stop it; a listing that hid that would
        # report gaps on a box nothing can edit.
        data = {
            "status": "Broken",
            "errors": ["ha.pid is stale", {"not": "a string"}, ""],
        }
        with fake_limactl(json.dumps(data)):
            for read in (yo.LIMA.config_gaps, yo.LIMA.keepalive_pid):
                with self.assertRaises(yo.YoError) as caught:
                    read()
                message = caught.exception.message
                self.assertIn("reports yolobox as Broken: ha.pid is stale", message)
                self.assertIn("limactl list yolobox", message)
                self.assertNotIn("delete", message.replace("Do not delete", ""))
            # The status itself stays readable: the callers that decide by it
            # (the waits for the VM and its sshd) say where to look themselves.
            self.assertEqual(
                yo.LIMA.vm_status(), yo.VmStatus(yo.VmState.OTHER, "Broken")
            )

    def test_a_status_yo_has_no_name_for_is_another_state_shown_in_its_words(self):
        data = {"status": "lizard", "config": {"ssh": {}, "user": {}}}
        with fake_limactl(json.dumps(data)):
            self.assertEqual(
                yo.LIMA.vm_status(), yo.VmStatus(yo.VmState.OTHER, "lizard")
            )

    def test_an_instance_without_a_status_word_is_an_error(self):
        for status in (MISSING, None, "", 1):
            with self.subTest(status=status):
                data = {"config": {"ssh": {}, "user": {}}}
                if status is not MISSING:
                    data["status"] = status
                with fake_limactl(json.dumps(data)):
                    with self.assertRaisesRegex(yo.YoError, "not a word"):
                        yo.LIMA.vm_status()

    def test_a_broken_instance_without_a_reason_is_still_an_error(self):
        for errors in (MISSING, None, [], [{}], "lizard"):
            with self.subTest(errors=errors):
                data = {"status": "Broken", "config": {}}
                if errors is not MISSING:
                    data["errors"] = errors
                with fake_limactl(json.dumps(data)):
                    with self.assertRaisesRegex(
                        yo.YoError, "as Broken and prints no reason"
                    ):
                        yo.LIMA.config_gaps()

    def test_an_instance_that_only_ignores_dhcp_still_mirrors_udp_listeners(self):
        # Older instances ignore UDP 68 alone; tailscaled's 41641 is still
        # mirrored onto the Mac, where a host's own Tailscale may hold it.
        dhcp_only = self.udp_ignore_rule(
            guestPortRange=[68, 68], guestIPMustBeZero=True
        )
        with self.mock_lima_list(self.replacing_udp_rule(dhcp_only)):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_with_lima_default_guest_ip_still_mirrors_wildcard_listeners(
        self,
    ):
        # Lima skips an ignore rule for a listener on 0.0.0.0 or :: unless the
        # rule's guestIP is unspecified: tailscaled's 41641 and DHCP's 68 stay.
        loopback_only = self.udp_ignore_rule(guestIP="127.0.0.1")
        with self.mock_lima_list(self.replacing_udp_rule(loopback_only)):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_pinned_to_exactly_zero_still_mirrors_loopback_listeners(
        self,
    ):
        # guestIPMustBeZero makes 0.0.0.0 an exact match, so 127.0.0.1 and
        # specific-address listeners fall through to the default forward.
        exact_zero = self.udp_ignore_rule(guestIPMustBeZero=True)
        with self.mock_lima_list(self.replacing_udp_rule(exact_zero)):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_with_garbage_guest_ip_is_a_gap(self):
        for guest_ip in ("lizard", "", None, 0):
            with self.subTest(guest_ip=guest_ip):
                rule = self.udp_ignore_rule(guestIP=guest_ip)
                with self.mock_lima_list(self.replacing_udp_rule(rule)):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_rule_spelled_unlike_the_restate_is_a_gap_even_when_it_covers_all(
        self,
    ):
        # "::" ignores every listener as 0.0.0.0 does, but the check compares
        # the instance with what the restate writes, and the restate is the
        # remedy either way.
        rule = self.udp_ignore_rule(guestIP="::")
        with self.mock_lima_list(self.replacing_udp_rule(rule)):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_forward_before_the_catch_all_is_a_gap(self):
        # Lima takes the first matching rule, so the catch-all after it
        # ignores nothing on that port: 41641 is mirrored onto the Mac.
        forward = {
            "proto": "udp",
            "guestIP": "127.0.0.1",
            "guestIPMustBeZero": False,
            "guestPortRange": [41641, 41641],
            "hostIP": "0.0.0.0",
            "hostPortRange": [41641, 41641],
        }
        forwards = [forward, *self.restated_forwards()]
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_UDP_FORWARDS])

    def test_a_forward_of_any_protocol_before_the_catch_alls_is_both_gaps(self):
        forwards = [{"guestPort": 4321}, *self.restated_forwards()]
        self.assertEqual(
            self.gaps_for(forwards), [yo.GAP_UDP_FORWARDS, yo.GAP_TCP_FORWARDS]
        )

    def test_a_blanket_udp_rule_that_forwards_instead_of_ignoring_is_a_gap(self):
        forwarding = self.udp_ignore_rule(ignore=False)
        with self.mock_lima_list(self.replacing_udp_rule(forwarding)):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_reads_host_agent_pid_from_the_same_listing(self):
        with self.mock_lima_list([], host_agent_pid=999):
            self.assertEqual(yo.box.driver.keepalive_pid(), 999)

    def test_a_host_agent_pid_that_is_not_a_real_pid_is_none(self):
        for value in BAD_PIDS:
            with self.subTest(value=value):
                with self.mock_lima_list([], host_agent_pid=value):
                    self.assertIsNone(yo.box.driver.keepalive_pid())

    def test_a_mount_is_reported(self):
        mounts = [{"location": "/Users/x", "writable": True}]
        with self.mock_lima_list(self.restated_forwards(), mounts=mounts):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_MOUNTS])

    def test_an_empty_or_omitted_mount_list_is_no_gap(self):
        # Lima leaves an empty list out of its JSON.
        for mounts in ([], None):
            with self.subTest(mounts=mounts):
                with self.mock_lima_list(self.restated_forwards(), mounts=mounts):
                    self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_mounts_that_are_not_a_list_are_a_gap_not_a_clean_bill(self):
        for mounts in ("lizard", {"location": "/Users/x"}, 0, True, ""):
            with self.subTest(mounts=mounts):
                with self.mock_lima_list(self.restated_forwards(), mounts=mounts):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_MOUNTS])

    def test_forward_agent_is_reported(self):
        with self.mock_lima_list(self.restated_forwards(), forward_agent=True):
            self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_FORWARD_AGENT])

    def test_forward_agent_false_or_a_string_is_no_gap(self):
        for value in (False, "true"):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), forward_agent=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_ssh_over_vsock_left_at_lima_default_is_reported(self):
        for value in (None, True):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), over_vsock=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_SSH_OVER_VSOCK])

    def test_host_proxy_env_propagation_is_reported_unless_pinned_off(self):
        # An instance that predates the pin has true; an absent key falls back
        # to lima's default (true), so anything but a literal false is a gap.
        for value in (True, None, MISSING, "lizard", "false", 0):
            with self.subTest(value=value):
                with self.mock_lima_list(
                    self.restated_forwards(), propagate_proxy_env=value
                ):
                    self.assertEqual(
                        yo.LIMA.config_gaps(), [yo.GAP_PROPAGATE_PROXY_ENV]
                    )

    def test_host_ssh_pubkey_loading_is_reported_unless_pinned_off(self):
        for value in (True, None, MISSING, "lizard", "false", 0):
            with self.subTest(value=value):
                with self.mock_lima_list(
                    self.restated_forwards(), load_dot_ssh_pub_keys=value
                ):
                    self.assertEqual(
                        yo.LIMA.config_gaps(), [yo.GAP_LOAD_DOT_SSH_PUB_KEYS]
                    )

    def test_an_operator_uid_that_is_not_501_or_not_reported_is_a_gap(self):
        # Lima reads user.uid only when it creates the instance.
        for value in (502, 0, None, MISSING, "501", 501.5, True, [501]):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), user_uid=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_OPERATOR_UID])
        with self.mock_lima_list(self.restated_forwards(), user_uid=501):
            self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_a_vm_type_other_than_vz_is_a_gap(self):
        # Only vz has the rosetta share, and lima reads vmType at creation.
        for value in ("qemu", None, 1):
            with self.subTest(vm_type=value):
                with self.mock_lima_list(self.restated_forwards(), vm_type=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_VM_TYPE])
        with self.mock_lima_list(self.restated_forwards(), vm_type="vz"):
            self.assertEqual(yo.LIMA.config_gaps(), [])

    def udp_ignore_rule(self, **overrides):
        rule = {
            "proto": "udp",
            "guestIP": "0.0.0.0",
            "guestIPMustBeZero": False,
            "guestPortRange": [1, 65535],
            "ignore": True,
        }
        rule.update(overrides)
        return rule

    def restated_forwards(self):
        # `limactl list yolobox --json` .config.portForwards after
        # `yo bootstrap`'s restate, written out by hand from lima's defaults
        # (FillPortForwardDefaults): proto any, guestIP 127.0.0.1 (0.0.0.0 when
        # the rule says so), guestIPMustBeZero false, hostIP 127.0.0.1,
        # hostPortRange = guestPortRange (0-0 for a socket), `ignore` omitted
        # when false. {{.Home}} is expanded to the home directory.
        def ignored_tcp_range(first, last):
            return {
                "proto": "tcp",
                "guestIP": "0.0.0.0",
                "guestIPMustBeZero": False,
                "guestPortRange": [first, last],
                "hostIP": "127.0.0.1",
                "hostPortRange": [first, last],
                "ignore": True,
            }

        return [
            ignored_tcp_range(1455, 1457),
            ignored_tcp_range(8020, 8020),
            ignored_tcp_range(8085, 8085),
            ignored_tcp_range(9005, 9005),
            ignored_tcp_range(18000, 18000),
            {
                "proto": "tcp",
                "guestIP": "127.0.0.1",
                "guestIPMustBeZero": False,
                "guestPortRange": [1024, 49151],
                "hostIP": "127.0.0.1",
                "hostPortRange": [1024, 49151],
            },
            {
                "proto": "udp",
                "guestIP": "0.0.0.0",
                "guestIPMustBeZero": False,
                "guestPortRange": [1, 65535],
                "hostIP": "127.0.0.1",
                "hostPortRange": [1, 65535],
                "ignore": True,
            },
            {
                "proto": "tcp",
                "guestIP": "0.0.0.0",
                "guestIPMustBeZero": False,
                "guestPortRange": [1, 65535],
                "hostIP": "127.0.0.1",
                "hostPortRange": [1, 65535],
                "ignore": True,
            },
        ]

    def leftover_rule(self):
        # An instance from before the port range: one dev port, published on
        # every host address.
        return {
            "proto": "tcp",
            "guestIP": "127.0.0.1",
            "guestIPMustBeZero": False,
            "guestPortRange": [4321, 4321],
            "hostIP": "0.0.0.0",
            "hostPortRange": [4321, 4321],
        }

    def replacing_udp_rule(self, udp_rule):
        return [
            udp_rule if forward.get("proto") == "udp" else forward
            for forward in self.restated_forwards()
        ]

    def gaps_for(self, forwards):
        with self.mock_lima_list(forwards):
            return yo.LIMA.config_gaps()

    def test_a_restated_instance_has_no_gap(self):
        self.assertEqual(self.gaps_for(self.restated_forwards()), [])

    def test_an_instance_without_the_tcp_catch_all_has_the_tcp_gap(self):
        forwards = self.restated_forwards()[:-1]
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_TCP_FORWARDS])

    def test_a_leftover_rule_published_beyond_loopback_is_a_tcp_gap(self):
        forwards = [self.leftover_rule(), *self.restated_forwards()]
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_TCP_FORWARDS])

    def test_an_instance_missing_one_oauth_ignore_has_the_tcp_gap(self):
        forwards = [
            forward
            for forward in self.restated_forwards()
            if forward.get("guestPortRange") != [8085, 8085]
        ]
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_TCP_FORWARDS])

    def test_an_instance_with_the_range_after_the_catch_all_has_the_tcp_gap(self):
        forwards = self.restated_forwards()
        forwards.append(forwards.pop(len(yo.OAUTH_CALLBACK_PORT_RANGES)))
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_TCP_FORWARDS])

    def test_a_tcp_catch_all_with_lima_default_guest_ip_is_a_gap(self):
        # It would match loopback listeners only; wildcard ones slip past.
        loopback_catch_all = {
            "proto": "tcp",
            "guestIP": "127.0.0.1",
            "guestIPMustBeZero": False,
            "guestPortRange": [1, 65535],
            "hostIP": "127.0.0.1",
            "hostPortRange": [1, 65535],
            "ignore": True,
        }
        forwards = [*self.restated_forwards()[:-1], loopback_catch_all]
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_TCP_FORWARDS])

    def test_an_instance_from_before_the_port_range_has_the_tcp_gap(self):
        old = [
            self.leftover_rule(),
            *(
                forward
                for forward in self.restated_forwards()
                if forward.get("proto") == "udp"
            ),
        ]
        self.assertEqual(self.gaps_for(old), [yo.GAP_TCP_FORWARDS])

    def test_a_socket_rule_the_yaml_does_not_have_is_a_gap(self):
        socket_rule = {
            "proto": "any",
            "guestIP": "127.0.0.1",
            "guestIPMustBeZero": False,
            "guestPortRange": [1, 65535],
            "guestSocket": yo.OP_GUEST_SOCK,
            "hostIP": "127.0.0.1",
            "hostPortRange": [0, 0],
            "hostSocket": "/somewhere/agent.sock",
            "reverse": True,
        }
        forwards = self.restated_forwards()
        forwards.insert(6, socket_rule)
        # One gap of its own: lima never matches a socket rule to a listener,
        # so the listener rules have no gap.
        self.assertEqual(self.gaps_for(forwards), [yo.GAP_REVERSE_SOCKETS])
        message = yo.LIMA.config_gap_message([yo.GAP_REVERSE_SOCKETS])
        self.assertIn("limactl --log-level=warn edit", message)
        self.assertNotIn("destroy", message)

    def test_no_usable_port_forwards_reports_every_gap_without_raising(self):
        for forwards in ([], None, ["lizard"], [None, 0, "lizard"]):
            with self.subTest(forwards=forwards):
                self.assertEqual(
                    self.gaps_for(forwards),
                    [yo.GAP_UDP_FORWARDS, yo.GAP_TCP_FORWARDS],
                )

    def test_missing_instance_ha_pid_is_none(self):
        with fake_limactl("", listed=[]):
            self.assertIsNone(yo.box.driver.keepalive_pid())


class TestNormalizePortForward(unittest.TestCase):
    def test_a_bare_guest_port_gets_lima_defaults(self):
        self.assertEqual(
            yo.normalize_port_forward({"guestPort": 4321}),
            {
                "proto": "any",
                "guestIP": "127.0.0.1",
                "guestIPMustBeZero": False,
                "guestPortRange": [4321, 4321],
                "hostIP": "127.0.0.1",
                "hostPortRange": [4321, 4321],
                "ignore": False,
            },
        )

    def test_a_wildcard_ignore_rule_must_match_zero_unless_told_otherwise(self):
        rule = yo.normalize_port_forward({"guestIP": "0.0.0.0", "ignore": True})
        self.assertIs(rule["guestIPMustBeZero"], True)
        self.assertEqual(rule["guestPortRange"], [1, 65535])
        self.assertEqual(rule["hostPortRange"], [1, 65535])
        self.assertIs(rule["ignore"], True)

    def test_must_be_zero_false_survives(self):
        rule = yo.normalize_port_forward(
            {"guestIP": "0.0.0.0", "guestIPMustBeZero": False}
        )
        self.assertIs(rule["guestIPMustBeZero"], False)

    def test_a_port_range_gives_the_host_the_same_range(self):
        rule = yo.normalize_port_forward({"guestPortRange": [1024, 49151]})
        self.assertEqual(rule["hostPortRange"], [1024, 49151])

    def test_normalizing_twice_changes_nothing(self):
        for forward in yo.LIMA_PORT_FORWARDS:
            with self.subTest(forward=forward):
                once = yo.normalize_port_forward(forward)
                self.assertEqual(yo.normalize_port_forward(once), once)

    def test_an_empty_rule_is_every_port_on_loopback(self):
        rule = yo.normalize_port_forward({})
        self.assertEqual(rule["guestPortRange"], [1, 65535])
        self.assertEqual(rule["guestIP"], "127.0.0.1")


class TestLimaConfigGapMessage(unittest.TestCase):
    def test_a_gap_lima_reads_only_at_creation_recreates_and_warns_of_the_loss(self):
        gaps = [yo.GAP_MOUNTS, yo.GAP_FORWARD_AGENT, yo.GAP_VM_TYPE]
        message = yo.LIMA.config_gap_message(gaps)
        for gap in gaps:
            self.assertIn(gap, message)
        self.assertIn("yo destroy --yes && yo bootstrap", message)
        self.assertIn("copy out what you need first", message)
        self.assertIn("deletes everything on it", message)
        self.assertNotIn("--set", message)

    def test_settings_limactl_edit_can_change_are_one_edit_of_the_stopped_box(self):
        message = yo.LIMA.config_gap_message(
            [yo.GAP_MOUNTS, yo.GAP_UDP_FORWARDS, yo.GAP_TCP_FORWARDS]
        )
        remedy = message.splitlines()[-1]
        self.assertTrue(remedy.startswith("fix it while it is stopped: yo down && "))
        (expression,) = [
            word
            for word in shlex.split(remedy.split(": ", 1)[1])
            if word.startswith(".mounts")
        ]
        # One restate covers both forward gaps.
        self.assertEqual(expression, ".mounts = [] | " + yo.lima.RESTATE_PORT_FORWARDS)
        self.assertNotIn("destroy", message)


class TestLimaStartRefusesOnlyTheGapsThatBreakThePromises(FakeHome):
    """An instance's settings are judged at its start: a gap that breaks a
    promise the box makes refuses the start, any other is a warning."""

    def start(self, gaps):
        warnings = []
        starter = mock.Mock()
        starter.poll.return_value = None
        starter.wait.return_value = 0
        with (
            mock.patch.object(yo.LIMA, "config_gaps", return_value=gaps),
            mock.patch.object(yo.lima, "lima_instance_exists", return_value=True),
            mock.patch.object(yo.proc, "spawn", return_value=starter) as spawn,
            mock.patch.object(yo.LIMA, "wait_until_running"),
            mock.patch.object(yo.unlock, "unlock_disk"),
            mock.patch.object(yo.unlock, "wait_for_operator"),
            mock.patch.object(yo.ui, "err", side_effect=warnings.append),
            yo.box_lock(wait=True) as held,
        ):
            try:
                yo.LIMA.start(held, lambda: b"pass")
            except yo.YoError as refused:
                return refused, spawn, warnings
        return None, spawn, warnings

    def test_ssh_over_vsock_or_a_host_mount_refuses_before_the_vm_is_touched(self):
        for gaps in (
            [yo.GAP_SSH_OVER_VSOCK],
            [yo.GAP_FORWARD_AGENT, yo.GAP_SSH_OVER_VSOCK],
            [yo.GAP_UDP_FORWARDS, yo.GAP_MOUNTS],
        ):
            with self.subTest(gaps=gaps):
                refused, spawn, _ = self.start(gaps)
                for gap in gaps:
                    self.assertIn(gap, refused.message)
                # Both are settings `limactl edit` undoes: nothing is lost.
                self.assertIn("limactl --log-level=warn edit", refused.message)
                self.assertNotIn("destroy", refused.message)
                spawn.assert_not_called()

    def test_every_other_gap_is_one_warning_and_the_start_goes_on(self):
        others = [
            yo.GAP_FORWARD_AGENT,
            yo.GAP_PROPAGATE_PROXY_ENV,
            yo.GAP_LOAD_DOT_SSH_PUB_KEYS,
            yo.GAP_UDP_FORWARDS,
            yo.GAP_TCP_FORWARDS,
            yo.GAP_OPERATOR_UID,
            yo.GAP_VM_TYPE,
        ]
        refused, spawn, warnings = self.start(others)
        self.assertIsNone(refused)
        spawn.assert_called_once()
        self.assertEqual(len(warnings), 1)
        for gap in others:
            self.assertIn(gap, warnings[0])

    def test_no_gap_prints_nothing(self):
        refused, spawn, warnings = self.start([])
        self.assertIsNone(refused)
        spawn.assert_called_once()
        self.assertEqual(warnings, [])


class TestHerdrMachineId(unittest.TestCase):
    def test_empty_listing_has_no_machine(self):
        self.assertIsNone(yo.herdr_machine_id("[]"))

    def test_the_matching_targets_id_is_returned(self):
        self.assertEqual(
            yo.herdr_machine_id(
                '[{"id": "7", "label": "x", "target": "other-box"},'
                ' {"id": "9", "label": "ops", "target": "yolobox", "enabled": true}]'
            ),
            "9",
        )

    def test_unrelated_target_is_no_machine(self):
        self.assertIsNone(yo.herdr_machine_id('[{"id": "1", "target": "other-box"}]'))

    def test_yolobox_value_outside_the_target_field_is_not_a_match(self):
        # Locks in the fix for a hidden failure: the old implementation matched
        # "yolobox" anywhere in an entry, so a machine merely *labelled*
        # yolobox but pointed at a different ssh target read as already added.
        self.assertIsNone(
            yo.herdr_machine_id(
                '[{"id": "1", "label": "yolobox", "target": "other-box"}]'
            )
        )

    def test_unparseable_json_raises(self):
        with self.assertRaisesRegex(
            yo.YoError, "herdr machine list --json produced unparseable"
        ):
            yo.herdr_machine_id("not json")

    def test_non_array_json_raises(self):
        with self.assertRaisesRegex(
            yo.YoError, "herdr machine list --json did not print a JSON"
        ):
            yo.herdr_machine_id('{"target": "yolobox"}')

    def test_a_matching_entry_without_a_usable_id_raises(self):
        for entry in (
            '{"target": "yolobox"}',
            '{"id": "", "target": "yolobox"}',
            '{"id": 5, "target": "yolobox"}',
            '{"id": null, "target": "yolobox"}',
        ):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(
                    yo.YoError, "has no id.*herdr machine list --json.*machine remove"
                ):
                    yo.herdr_machine_id("[%s]" % entry)


class TestStatusWithVmDown(unittest.TestCase):
    def test_unreachable_vm_is_reported_once_fails_the_run_and_no_guest_probe_runs(
        self,
    ):
        stdout = io.StringIO()
        guest_calls = []

        def ssh_run(role, argv, **kwargs):
            guest_calls.append(argv)
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="")

        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(
                yo.proc, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
            ),
            mock.patch.object(yo.sshx, "ssh_run", ssh_run),
            # The port sends no banner: nothing listens.
            mock.patch.object(yo.unlock, "ssh_banner", lambda endpoint: ""),
            mock.patch.object(yo.shutil, "which", lambda name: None),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))

        self.assertEqual((caught.exception.message, caught.exception.code), (None, 1))
        self.assertEqual(guest_calls, [])
        self.assertEqual(stdout.getvalue().count("unreachable"), 1)
        self.assertIn("yo up", stdout.getvalue())


class TestStatusLooksInsideAnInitrdOnlyUnderTheBoxLock(FakeHome):
    """`yo status` asks root nothing while another yo acts on the box, and an
    answer on the port that is no ssh at all is a warning, not an abort."""

    INITRD = "SSH-2.0-OpenSSH_10.6 %s\r" % yo.INITRD_SSH_BANNER_MARK

    def status(self, banner):
        stdout = io.StringIO()
        logins = []

        def run(argv, **kwargs):
            if argv[0] == "ssh":
                logins.append(argv)
                return subprocess.CompletedProcess(argv, 0, "ask=pending\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(yo.LIMA, "show_status", return_value=None),
            mock.patch.object(yo.LIMA, "disk_image", return_value=None),
            mock.patch.object(yo.LIMA, "ssh_port", return_value=60022),
            mock.patch.object(yo.proc, "run", run),
            mock.patch.object(yo.unlock, "ssh_banner", lambda endpoint: banner),
            hostagent_holds_the_port(),
            mock.patch.object(yo.shutil, "which", lambda name: None),
            self.assertRaises(yo.YoError) as caught,
        ):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))
        self.assertEqual(caught.exception.code, 1)
        return stdout.getvalue(), logins

    def test_a_box_in_its_initrd_is_looked_inside_as_root_under_the_lock(self):
        out, logins = self.status(self.INITRD)
        self.assertEqual(len(logins), 2, logins)  # the root probe and the ask probe
        self.assertTrue(all("User=root" in argv for argv in logins))
        self.assertIn("waiting for the passphrase", out)
        self.assertFalse(box_lock_is_held())

    def test_a_box_in_its_initrd_that_another_yo_acts_on_is_not_asked_as_root(self):
        with box_lock_held_elsewhere():
            out, logins = self.status(self.INITRD)
        self.assertEqual(logins, [])
        self.assertIn("another yo is acting on it", out)

    def test_a_port_that_answers_no_ssh_is_a_warning_and_the_report_goes_on(self):
        out, logins = self.status("lizard")
        self.assertEqual(logins, [])
        self.assertIn("answers lizard, which is no ssh identification", out)
        self.assertIn("aws: disabled", out)


# How long a test's fake server waits for the connection the code under test
# should make; a run that never connects ends the server, not the suite.
ACCEPT_TIMEOUT_SECONDS = 10


class TestBootStageNeverOffersStage2ARoot(unittest.TestCase):
    """boot_stage reads a real TCP banner and asks for root only off the
    initrd's: root on any stage 2 is an invalid user, which sshd penalises per
    source address, and every host connection shares the gateway's address."""

    STAGE2 = "SSH-2.0-OpenSSH_10.6 %s\r\n" % yo.STAGE2_SSH_BANNER_MARK
    INITRD = "SSH-2.0-OpenSSH_10.6 %s\r\n" % yo.INITRD_SSH_BANNER_MARK

    def serve(self, banner):
        """A port whose sshd sends `banner`, or that nothing listens on (None).
        The initrd's banner is read twice: once to tell the sshds apart, and
        once more at the gate of the root login."""
        reads = 2 if banner == self.INITRD else 1
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        if banner is None:
            server.close()
            return port
        self.addCleanup(server.close)
        server.listen(1)
        # Bounded, so a boot_stage that never connects fails its test instead
        # of hanging the suite in the cleanup's join.
        server.settimeout(ACCEPT_TIMEOUT_SECONDS)

        def answer():
            for _ in range(reads):
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    return
                with conn:
                    conn.sendall(banner.encode())

        thread = threading.Thread(target=answer)
        thread.start()
        self.addCleanup(thread.join)
        return port

    def boot_stage(
        self, banner, operator=yo.SshOutcome.REFUSED, root=yo.SshOutcome.ANSWERED
    ):
        """The stage's kind and the accounts asked; the stage is self.stage."""
        port = self.serve(banner)
        asked = []

        def probe(argv, user):
            asked.append(user)
            outcome = operator if user != "root" else root
            return yo.unlock.SshAttempt(outcome, "%s's words" % user)

        with (
            mock.patch.object(
                yo.LIMA, "ssh_facts", return_value=yo.SshFacts(port, "op", "/k", "none")
            ),
            # The test's port is whatever the OS gave, maybe in Lima's mirrored range.
            mock.patch.object(yo.LIMA, "verify_vm_listener"),
            mock.patch.object(yo.unlock, "ssh_attempt", probe),
            mock.patch.object(yo.release, "host_user", return_value="op"),
            mock.patch.object(socket, "create_connection", REAL_CREATE_CONNECTION),
            yo.box_lock(wait=True) as held,
        ):
            self.stage = yo.boot_stage(held)
            return self.stage.kind, asked

    def test_a_marked_stage2_that_refuses_the_operator_is_never_asked_for_root(self):
        self.assertEqual(self.boot_stage(self.STAGE2), (yo.BootStage.NONE, ["op"]))
        self.assertEqual(
            self.stage.said,
            "its sshd did not let the operator in; ssh said: op's words",
        )

    def test_a_port_that_answers_no_ssh_line_is_named_with_its_words(self):
        # Something else holds the port: no login at all, and the error quotes it.
        for banner in ("lizard\r\n", "SSH-1.5-ancient\r\n", "\x1b[31mred\r\n"):
            with self.subTest(banner=banner):
                with self.assertRaises(yo.YoError) as caught:
                    self.boot_stage(banner)
                self.assertIn(
                    "answers %s, which is no ssh identification"
                    % yo.ui.shown(banner.split("\n")[0]),
                    caught.exception.message,
                )

    def test_the_initrds_banner_is_asked_for_root_alone(self):
        self.assertEqual(self.boot_stage(self.INITRD), (yo.BootStage.INITRD, ["root"]))

    def test_an_unmarked_sshd_is_the_seeds_stage2_and_never_asked_for_root(self):
        # The seed image's stage 2, or a word that only resembles a mark.
        for banner in (
            "SSH-2.0-OpenSSH_10.6\r\n",
            "SSH-2.0-OpenSSH_10.6 %s-old\r\n" % yo.STAGE2_SSH_BANNER_MARK,
            "SSH-2.0-OpenSSH_10.6 %s-old\r\n" % yo.INITRD_SSH_BANNER_MARK,
        ):
            with self.subTest(banner=banner):
                self.assertEqual(
                    self.boot_stage(banner, operator=yo.SshOutcome.ANSWERED),
                    (yo.BootStage.STAGE2, ["op"]),
                )
                self.assertEqual(self.boot_stage(banner), (yo.BootStage.NONE, ["op"]))

    def test_an_initrd_that_refuses_root_is_unknown_not_absent(self):
        # A dead 1Password in front of an initrd looks exactly like this.
        self.assertEqual(
            self.boot_stage(self.INITRD, root=yo.SshOutcome.REFUSED),
            (yo.BootStage.UNKNOWN, ["root"]),
        )
        self.assertIn("(ssh said: root's words)", yo.unknown_stage_message(self.stage))

    def test_an_empty_banner_offers_no_login_at_all(self):
        # Nothing listening, or an sshd that sent nothing in time: whoever it
        # is, it was not shown root, whatever a login would have answered.
        for banner in (None, ""):
            with self.subTest(banner=banner):
                self.assertEqual(self.boot_stage(banner), (yo.BootStage.NONE, []))

    def test_a_stage2_that_admits_the_operator_is_stage2_off_an_unmarked_banner(self):
        # Built before the mark: the operator's login is what tells it apart.
        self.assertEqual(
            self.boot_stage(
                "SSH-2.0-OpenSSH_10.6\r\n", operator=yo.SshOutcome.ANSWERED
            ),
            (yo.BootStage.STAGE2, ["op"]),
        )


class TestWaitForOperatorRetriesEveryShapeUntilTheDeadline(unittest.TestCase):
    """A first boot's sshd listens late (mkswap writes the swapfile first), and
    in that window a probe came back once as RAN and once as ERROR: both are
    retried, like a refused key or a closed port, until the deadline."""

    def wait(self, outcomes, status=yo.VmStatus(yo.VmState.RUNNING, "running")):
        outcomes = iter(outcomes)
        clock = VirtualClock()
        driver = mock.Mock()
        driver.vm_status.return_value = status
        driver.hints = yo.LIMA.hints
        box = mock.Mock()
        box.name = "e2e1"

        def probe(endpoint):
            outcome = next(outcomes)
            return outcome, "ssh said %s" % outcome.value

        with (
            mock.patch.object(yo.release, "host_user", return_value="op"),
            mock.patch.object(yo.unlock, "probe_operator", probe),
            mock.patch.object(yo.state, "driver", return_value=driver),
            mock.patch.object(yo.state, "box", box),
            mock.patch.object(yo.state, "yo_cmd", lambda *a: "yo -m e2e1 status"),
            clock,
        ):
            yo.unlock.wait_for_operator()
        return clock.sleeps

    def test_every_failed_shape_is_retried_until_the_operator_answers(self):
        sleeps = self.wait(
            [
                yo.SshOutcome.RAN,
                yo.SshOutcome.ERROR,
                yo.SshOutcome.NOT_LISTENING,
                yo.SshOutcome.REFUSED,
                yo.SshOutcome.ANSWERED,
            ]
        )
        self.assertEqual(sleeps, [yo.OPERATOR_POLL_SECONDS] * 4)

    def test_a_shape_that_never_changes_is_an_error_at_the_deadline_quoting_it(self):
        for outcome in (yo.SshOutcome.ERROR, yo.SshOutcome.RAN):
            with self.subTest(outcome=outcome.value):
                with self.assertRaises(yo.YoError) as caught:
                    self.wait(itertools.repeat(outcome))
                message = caught.exception.message
                self.assertIn(
                    "e2e1 is not reachable as the operator after %d s (the last ssh "
                    "attempt: %s:" % (yo.OPERATOR_WAIT_SECONDS, outcome.value),
                    message,
                )
                self.assertIn("ssh said %s" % outcome.value, message)
                self.assertIn("yo -m e2e1 status", message)

    def test_the_wait_lasts_the_deadline_and_no_longer(self):
        probes = []

        def counting():
            while True:
                probes.append(1)
                yield yo.SshOutcome.ERROR

        with self.assertRaises(yo.YoError):
            self.wait(counting())
        waited = (len(probes) - 1) * yo.OPERATOR_POLL_SECONDS
        self.assertGreater(waited, yo.OPERATOR_WAIT_SECONDS)
        self.assertLessEqual(
            waited, yo.OPERATOR_WAIT_SECONDS + yo.OPERATOR_POLL_SECONDS
        )

    def test_a_domain_that_went_away_ends_the_wait_at_once(self):
        with self.assertRaisesRegex(yo.YoError, "e2e1 is shut off, not Running"):
            self.wait(
                itertools.repeat(yo.SshOutcome.ERROR),
                status=yo.VmStatus(yo.VmState.OFF, "shut off"),
            )


class TestUnlockDiskSendsThePassphraseOnlyToTheInitrd(unittest.TestCase):
    """The passphrase goes to root over an unpinned channel, so the banner is
    read before every helper run: only the initrd's banner gets the helper,
    and any other sshd (a stage 2, the seed image's unmarked one) is asked for
    the operator and nothing else."""

    DROPPED = yo.HelperRun(yo.SSH_UNREACHABLE, "", True)

    def run_unlock(self, kinds, operator_answers, helper_runs):
        """unlock_disk with `kinds` the sshd each round finds; the helper
        stands in for run_initrd_helper and derives the passphrase as it does."""
        kinds = iter(kinds)
        operator_answers = iter(operator_answers)
        helper_runs = iter(helper_runs)
        helper_calls = []
        self.derived = 0
        driver = mock.Mock()
        driver.vm_status.return_value = yo.VmStatus(yo.VmState.RUNNING, "Running")

        def passphrase():
            self.derived += 1
            return b"secret"

        def helper(gate, source):
            helper_calls.append(source())
            return next(helper_runs)

        gate = proved_initrd(yo.SshFacts(60022, "op", "/k/user", "none"))
        with (
            mock.patch.object(
                yo.unlock,
                "endpoint_kind",
                lambda endpoint: yo.unlock.Banner(next(kinds), "SSH-2.0-x"),
            ),
            # The gate reads the banner again; these rounds are about the first read.
            mock.patch.object(yo.unlock, "initrd_endpoint", lambda held: gate),
            mock.patch.object(
                yo.unlock,
                "probe_operator",
                lambda endpoint: yo.unlock.SshAttempt(next(operator_answers), ""),
            ),
            mock.patch.object(yo.unlock, "run_initrd_helper", helper),
            mock.patch.object(yo.state, "driver", return_value=driver),
            mock.patch.object(yo.state, "box", mock.Mock(name="box")),
            mock.patch.object(yo.ui, "err"),
            mock.patch.object(yo.time, "sleep"),
            yo.box_lock(wait=True) as held,
        ):
            yo.unlock_disk(held, passphrase)
        return helper_calls

    def test_after_the_session_dropped_a_stage2_never_sees_the_helper(self):
        helper_calls = self.run_unlock(
            kinds=[yo.Endpoint.INITRD, yo.Endpoint.NONE] + [yo.Endpoint.STAGE2] * 2,
            # The new stage 2 refuses the operator once (its key not written
            # yet): that round gets no helper either, only the next probe.
            operator_answers=[yo.SshOutcome.REFUSED, yo.SshOutcome.ANSWERED],
            helper_runs=[self.DROPPED],
        )
        self.assertEqual(helper_calls, [b"secret"])

    def test_a_stage2_that_refuses_the_operator_is_retried_without_root(self):
        helper_calls = self.run_unlock(
            kinds=[yo.Endpoint.STAGE2, yo.Endpoint.STAGE2],
            operator_answers=[yo.SshOutcome.REFUSED, yo.SshOutcome.ANSWERED],
            helper_runs=[],
        )
        self.assertEqual(helper_calls, [])

    def test_the_seeds_unmarked_sshd_boots_without_root_or_the_passphrase(self):
        helper_calls = self.run_unlock(
            kinds=[yo.Endpoint.UNMARKED] * 2,
            operator_answers=[yo.SshOutcome.REFUSED, yo.SshOutcome.ANSWERED],
            helper_runs=[],
        )
        self.assertEqual(helper_calls, [])
        self.assertEqual(self.derived, 0)

    def test_a_helper_that_fails_in_a_way_ssh_names_is_an_error_with_its_words(self):
        # "Too many authentication failures", a bad key file: nothing a retry fixes.
        failed = yo.HelperRun(yo.SSH_UNREACHABLE, "Load key: invalid format", False)
        with self.assertRaisesRegex(yo.YoError, "Load key: invalid format"):
            self.run_unlock(
                kinds=[yo.Endpoint.INITRD], operator_answers=[], helper_runs=[failed]
            )

    def test_nothing_listening_gets_no_helper_and_no_login(self):
        helper_calls = self.run_unlock(
            kinds=[yo.Endpoint.NONE, yo.Endpoint.STAGE2],
            operator_answers=[yo.SshOutcome.ANSWERED],
            helper_runs=[],
        )
        self.assertEqual(helper_calls, [])


LIMA_RUNNING = yo.VmStatus(yo.VmState.RUNNING, "Running")
LIMA_FORCE_STOP = "limactl stop --force yolobox"


class TestLimaStopHangOffersTheKillOnlyPastTheInitrd(unittest.TestCase):
    def test_the_kill_is_conditional_on_ssh_answering(self):
        with (
            mock.patch.object(yo.LIMA, "vm_status", return_value=LIMA_RUNNING),
            mock.patch.object(
                yo.proc, "run", side_effect=yo.RunTimeout("limactl stop timed out")
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                locked(yo, yo.LIMA.stop)
        message = caught.exception.message
        # "answers" would admit the initrd's host-key or permission errors.
        self.assertIn(
            "If yo ssh true succeeds (exits 0 and prints nothing), the box is past "
            "its initrd",
            message,
        )
        self.assertIn("If it fails in any way", message)
        self.assertIn("do not kill the hostagent", message)
        self.assertNotIn(LIMA_FORCE_STOP, message)


class TestLimaStopThatExitsNonZeroOnARunningBox(unittest.TestCase):
    """lima's stop has its own deadline and exits 1 past it, with the box still
    running: the same hang, so the same way out."""

    def failed_stop(self, stop, statuses):
        failed = subprocess.CompletedProcess(["limactl", "stop"], 1, None, None)
        with (
            mock.patch.object(yo.LIMA, "vm_status", side_effect=statuses),
            mock.patch.object(yo.subprocess, "run", return_value=failed),
        ):
            with self.assertRaises(yo.StopRefused) as caught:
                locked(yo, stop)
        return caught.exception

    def test_a_polite_stop_carries_the_hang_recovery_and_its_exit(self):
        refused = self.failed_stop(yo.LIMA.stop, [LIMA_RUNNING, LIMA_RUNNING])
        self.assertEqual(
            refused.reason, "limactl --log-level=warn stop yolobox failed (exit 1)"
        )
        self.assertIn("If yo ssh true succeeds", refused.message)
        self.assertIn("do not kill the hostagent", refused.message)

    def test_a_forced_stop_carries_the_hostagent_kill_alone(self):
        refused = self.failed_stop(yo.LIMA.force_stop, [LIMA_RUNNING])
        self.assertEqual(
            refused.reason,
            "limactl --log-level=warn stop --force yolobox failed (exit 1)",
        )
        self.assertIn("kill -9 any 'limactl hostagent'", refused.message)
        self.assertNotIn("systemctl reboot", refused.message)


class TestLimaInitrdTimeoutNamesItsBootLog(unittest.TestCase):
    def test_serialv_log_keeps_the_boot_so_it_is_named(self):
        last = yo.unlock.Round("no banner", yo.unlock.where_to_look())
        text = yo.unlock.initrd_timeout_error(last).message
        self.assertIn("did not let the operator in", text)
        self.assertIn("~/.lima/yolobox/serialv.log", text)
        self.assertIn("~/.lima/yolobox/ha.stderr.log", text)


class TestUnknownStageOffersNoForceOnLima(unittest.TestCase):
    def test_the_way_out_is_lima_s_key_and_never_a_forced_stop(self):
        text = yo.unlock.unknown_stage_message(yo.Stage(yo.BootStage.UNKNOWN, ""))
        self.assertIn("~/.lima/_config/user", text)
        self.assertIn("run the command again", text)
        self.assertNotIn(LIMA_FORCE_STOP, text)
        self.assertNotIn("--force", text)


class TestSshBannerReadsTheWholeLine(unittest.TestCase):
    """ssh_banner over one end of a socketpair: the other end sends the line in
    segments, each only after the client's previous recv returned, so the split
    is deterministic and needs no clock."""

    class Client:
        """The client end, which tells the server each time a recv returned."""

        def __init__(self, sock, received):
            self.sock = sock
            self.received = received

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.sock.close()

        def settimeout(self, timeout):
            self.sock.settimeout(timeout)

        def recv(self, size):
            data = self.sock.recv(size)
            self.received.set()
            return data

    def banner(self, *segments):
        client, server = socket.socketpair()
        received = threading.Event()

        def send():
            with server:
                for segment in segments:
                    received.clear()
                    server.sendall(segment)
                    # Bounded, so a client that stops reading ends the server.
                    received.wait(ACCEPT_TIMEOUT_SECONDS)

        thread = threading.Thread(target=send)
        thread.start()
        self.addCleanup(thread.join)
        with mock.patch.object(
            yo.unlock.socket,
            "create_connection",
            lambda address, timeout: self.Client(client, received),
        ):
            return yo.unlock.ssh_banner(yo.SshFacts(1, "op", "/k", "none"))

    def test_a_line_sent_in_two_segments_is_read_whole(self):
        mark = yo.STAGE2_SSH_BANNER_MARK.encode()
        self.assertEqual(
            self.banner(b"SSH-2.0-OpenSSH_10.6 yolo", mark[4:] + b"\r\n"),
            "SSH-2.0-OpenSSH_10.6 %s\r" % yo.STAGE2_SSH_BANNER_MARK,
        )

    def test_only_the_first_line_is_the_banner(self):
        self.assertEqual(
            self.banner(b"SSH-2.0-OpenSSH_10.6\r\nSSH-2.0-x yolobox-stage2\r\n"),
            "SSH-2.0-OpenSSH_10.6\r",
        )

    def test_a_line_without_a_newline_is_cut_at_the_rfc_cap(self):
        self.assertEqual(self.banner(b"x" * 200, b"y" * 200), "x" * 200 + "y" * 55)

    def test_a_peer_that_closes_early_leaves_what_it_sent(self):
        self.assertEqual(self.banner(b"SSH-2.0-Open"), "SSH-2.0-Open")

    def test_a_peer_that_closes_before_any_byte_sent_no_banner(self):
        with self.assertRaisesRegex(
            yo.unlock.NoBanner, "the connection closed before a line came"
        ):
            self.banner()


class TestSshBannerTellsNothingListeningFromAHostFailure(unittest.TestCase):
    def banner_when_connect_raises(self, failure):
        with mock.patch.object(
            yo.unlock.socket, "create_connection", side_effect=failure
        ):
            return yo.unlock.endpoint_kind(yo.SshFacts(60022, "op", "/k", "none"))

    def test_a_port_that_sends_no_line_is_no_banner_naming_what_came_instead(self):
        for failure, said in (
            (
                ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
                "no ssh banner: Connection refused",
            ),
            (
                ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
                "no ssh banner: Connection reset by peer",
            ),
            (
                socket.timeout("timed out"),
                "no ssh banner within %d s" % yo.sshconfig.SSH_CONNECT_TIMEOUT_SECONDS,
            ),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(
                    self.banner_when_connect_raises(failure),
                    yo.unlock.Banner(yo.Endpoint.NONE, said),
                )

    def test_a_host_that_cannot_connect_at_all_is_an_error_not_a_silent_port(self):
        for code in (errno.EMFILE, errno.EPERM):
            with self.subTest(errno=errno.errorcode[code]):
                with self.assertRaisesRegex(
                    yo.YoError, "cannot read the ssh banner on 127.0.0.1:60022"
                ):
                    self.banner_when_connect_raises(OSError(code, os.strerror(code)))


class TestTheBoxLockIsTakenOnceAndHandedDown(FakeHome):
    """flock is not re-entrant across two descriptors of one process, so a
    box_lock() inside another would wait on itself: it is refused at once."""

    def test_taking_the_lock_inside_itself_is_refused_at_once(self):
        for wait in (True, False):
            with self.subTest(wait=wait):
                with yo.box_lock(wait=True):
                    with self.assertRaisesRegex(RuntimeError, "inside itself"):
                        with yo.box_lock(wait=wait):
                            pass
                    self.assertTrue(box_lock_is_held())
                self.assertFalse(box_lock_is_held())

    def test_another_boxs_lock_may_be_held_beside_it(self):
        with yo.box_lock(wait=True):
            yo.set_box(yo.box.driver, "e2e1")
            try:
                with yo.box_lock(wait=False):
                    self.assertTrue(box_lock_is_held())
            finally:
                yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)
            self.assertTrue(box_lock_is_held())

    def test_the_lock_is_free_again_after_a_step_that_raised(self):
        with self.assertRaises(yo.YoError):
            with yo.box_lock(wait=True):
                raise yo.YoError("a step failed")
        with yo.box_lock(wait=False):
            pass

    def test_only_box_lock_hands_out_the_proof_that_it_is_held(self):
        with self.assertRaisesRegex(TypeError, "only state.box_lock"):
            yo.state.HeldBox(object())

    HOLDERS = (
        "yo up, yo bootstrap, yo disk-grow, yo down, yo destroy --yes or yo status"
    )

    def test_a_wait_for_another_yo_is_said_in_one_line_before_it_blocks(self):
        # The line is the moment to let the other yo go: the wait then ends.
        path = yo.state.box_lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        other = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(other, fcntl.LOCK_EX)
        said = []

        def err(line):
            said.append(line)
            os.close(other)

        with mock.patch.object(yo.ui, "err", side_effect=err):
            with yo.box_lock(wait=True):
                self.assertTrue(box_lock_is_held())
        self.assertEqual(
            said,
            [
                "yo: another yo is acting on yolobox (%s holds its lock); waiting "
                "for it" % self.HOLDERS
            ],
        )

    def test_a_lock_nobody_holds_is_taken_without_a_word(self):
        with mock.patch.object(yo.ui, "err") as err:
            with yo.box_lock(wait=True):
                pass
        err.assert_not_called()

    def test_a_command_that_will_not_wait_names_every_command_that_may_hold_it(self):
        with box_lock_held_elsewhere():
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_down(argparse.Namespace())
        self.assertIn(
            "another yo is acting on yolobox (%s holds its lock)" % self.HOLDERS,
            caught.exception.message,
        )


class TestSshArgv(FakeHome):
    def test_the_multiplexing_off_by_default_says_so(self):
        argv = yo.sshx.guest_argv(yo.OPERATOR, ["true"], stdin=False)
        self.assertIn("ControlPath=none", argv)


class TestLimaProvesItsHostagentHoldsTheSshPort(unittest.TestCase):
    """A login on the box's port, root's included, runs only while lsof names
    this instance's hostagent as its one listener: any process of the host
    user's can bind a port nothing holds."""

    def verify(self, lsof, hostagent=HOSTAGENT_PID, port=60022):
        """verify_vm_listener(port) with lsof ending as `lsof` (exit,
        stdout, stderr); the argv lsof ran with, or the refusal's message."""
        self.ran = []

        def run(argv, **kwargs):
            self.ran.append(list(argv))
            return subprocess.CompletedProcess(argv, *lsof)

        with (
            mock.patch.object(yo.LIMA, "keepalive_pid", return_value=hostagent),
            mock.patch.object(yo.proc, "run", side_effect=run),
        ):
            try:
                yo.LIMA.verify_vm_listener(port)
            except yo.YoError as refused:
                return refused.message
        return None

    def test_the_hostagent_alone_on_the_port_is_the_vms_listener(self):
        held = (0, "p%d\nf7\n" % HOSTAGENT_PID, "")
        self.assertIsNone(self.verify(held))
        self.assertEqual(
            self.ran, [["lsof", "-nP", "-iTCP:60022", "-sTCP:LISTEN", "-Fp"]]
        )

    def test_any_other_holder_is_refused_naming_its_pid_and_the_lookup(self):
        for listing in ("p77\nf3\n", "p%d\nf7\np77\nf3\n" % HOSTAGENT_PID):
            with self.subTest(listing=listing):
                message = self.verify((0, listing, ""))
                self.assertIn("127.0.0.1:60022 is held by pid 77", message)
                self.assertIn("lsof -nP -iTCP:60022 -sTCP:LISTEN", message)

    def test_a_listener_with_no_hostagent_running_is_a_strangers(self):
        message = self.verify((0, "p77\nf3\n", ""), hostagent=None)
        self.assertIn("held by pid 77, not by yolobox's hostagent (none runs)", message)

    def test_an_answer_that_names_no_listener_is_an_error_in_lsofs_words(self):
        # lsof exits 1 with no output when nothing listens, and for other
        # reasons too: nothing proves the port the VM's.
        for lsof, said in (
            ((1, "", ""), "failed (exit 1) with no output"),
            ((1, "", "lsof: WARNING: lizard"), "lsof: WARNING: lizard"),
            ((0, "", ""), "printed no listener's pid but ''"),
            ((0, "p12x\n", ""), "printed no listener's pid but 'p12x'"),
            ((0, "p%d\nlizard\n" % HOSTAGENT_PID, ""), "lizard"),
            ((0, "f7\n", ""), "printed no listener's pid but 'f7'"),
        ):
            with self.subTest(lsof=lsof):
                message = self.verify(lsof)
                self.assertIn(
                    "cannot prove that yolobox's hostagent holds 127.0.0.1:60022, "
                    "so yo does not connect",
                    message,
                )
                self.assertIn(said, message)

    def test_a_port_another_box_can_claim_is_refused_before_lsof_is_asked(self):
        message = self.verify((0, "p%d\n" % HOSTAGENT_PID, ""), port=22222)
        self.assertIn("ssh.localPort 22222", message)
        self.assertEqual(self.ran, [])


class TestPortOnLima(unittest.TestCase):
    def test_yo_port_exits_with_the_usage_code_because_lima_mirrors_ports_itself(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.LIMA.forward_port(8080)
        self.assertEqual(caught.exception.code, 64)


class TestNumbersFromOtherSystemsAreAsciiDigitsOnly(FakeHome):
    # "\u00b2".isdigit() is True, but int("\u00b2") raises ValueError.
    def root_usage_of(self, kib_line, human_line="/dev/x 1G 1G 1G 1% /"):
        completed = mock.Mock(
            returncode=0, stdout=df_output(kib_line, human_line), stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=completed) as fake_run:
            usage = yo.guest.guest_root_usage()
        fake_run.assert_called_once_with(yo.OPERATOR, yo.guest.ROOT_USAGE_PROBE)
        return usage

    def test_a_df_column_that_is_not_ascii_digits_is_unreadable(self):
        for kib_line in ("/dev/x \u00b2 1", "/dev/x 12 \uff11", "/dev/x 12", ""):
            with self.subTest(kib_line=kib_line):
                with self.assertRaisesRegex(yo.YoError, "no sizes in"):
                    self.root_usage_of(kib_line)
        usage = self.root_usage_of("/dev/x 12 1", "/dev/x 12K 1K 11K 8% /")
        self.assertEqual(
            (usage.human_line, usage.total_kib, usage.used_kib),
            ("/dev/x 12K 1K 11K 8% /", 12, 1),
        )

    def test_a_failed_ssh_or_df_is_an_error_with_what_it_said(self):
        for returncode, stderr in ((255, "ssh: connect refused"), (1, "df: /: gone")):
            with self.subTest(returncode=returncode):
                completed = yo.proc.GuestRun(["ssh"], returncode, "", stderr)
                with mock.patch.object(yo.sshx, "ssh_run", return_value=completed):
                    with self.assertRaisesRegex(
                        yo.YoError,
                        r"root usage from df \(exit %d\):\n  \| %s"
                        % (returncode, stderr),
                    ):
                        yo.guest.guest_root_usage()

    def test_a_df_that_prints_other_than_two_tables_is_an_error(self):
        for stdout in ("", "garbage\n", df_output() + "extra\n"):
            with self.subTest(stdout=stdout):
                completed = mock.Mock(returncode=0, stdout=stdout, stderr="")
                with mock.patch.object(yo.sshx, "ssh_run", return_value=completed):
                    with self.assertRaisesRegex(yo.YoError, "expected 4 lines"):
                        yo.guest.guest_root_usage()

    def test_a_limactl_size_that_is_not_ascii_digits_is_an_error(self):
        with mock.patch.object(
            yo.proc,
            "run",
            return_value=mock.Mock(returncode=0, stdout="\u00b2\n"),
        ):
            with self.assertRaisesRegex(yo.YoError, "reports no disk size"):
                yo.LIMA.disk_gib()


class TestDuKib(FakeHome):
    def test_a_missing_image_is_none(self):
        self.assertIsNone(yo.lima.du_kib(os.path.join(self.home, "disk")))

    def test_an_image_that_cannot_be_examined_is_an_error_naming_it(self):
        path = os.path.join(self.home, "disk")
        with mock.patch.object(
            yo.os, "stat", side_effect=PermissionError(13, "denied")
        ):
            with self.assertRaisesRegex(yo.YoError, "cannot stat %s: denied" % path):
                yo.lima.du_kib(path)


if __name__ == "__main__":
    unittest.main()
