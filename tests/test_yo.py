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
import json
import os
import select
import shutil
import socket
import stat
import socketserver
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "yolobox_cli"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_EGRESS = (FIXTURES / "egress.nft").read_text()
FIXTURE_EGRESS_TUNNEL = (FIXTURES / "egress-tunnel.nft").read_text()
FIXTURE_EMPTY_TABLE = "table inet yolobox-egress {\n}\n"
# A release this yo can build: at or above release.FIRST_HOST_OS_FREE_RELEASE.
A_RELEASE = "2.3.4"

# The host's CPU count in tests that create a box or read --cpus, so the default
# CPU count (min(4, host - 1)) and the --cpus range do not depend on the machine
# that runs them. FakeHome pins it; a test that needs another count patches
# os.cpu_count itself.
HOST_CPUS = 16


def host_cpus(count=HOST_CPUS):
    return mock.patch.object(os, "cpu_count", return_value=count)


REAL_CREATE_CONNECTION = socket.create_connection


def no_tcp_to_the_host(address, *args, **kwargs):
    raise ConnectionRefusedError(
        errno.ECONNREFUSED, "no test may reach a real TCP port: %s:%s" % address[:2]
    )


# yo's only TCP client is unlock.ssh_banner, and Lima's default port is the one
# a real box on the machine running the tests listens on. A test that does not
# fake the banner would meet that box's stage-2 sshd and take a different branch
# than on a machine with none. Refused is what the tests were written against;
# a test that serves its own port restores REAL_CREATE_CONNECTION.
mock.patch.object(socket, "create_connection", no_tcp_to_the_host).start()


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
    they do on a Mac.
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
    modules["state"].set_box(modules["lima"].LIMA, modules["config"].DEFAULT_INSTANCE)
    return YoNamespace(modules)


def unload_yo(name):
    """Forget the package `name` and every module under it."""
    for module_name in [
        key for key in sys.modules if key == name or key.startswith(name + ".")
    ]:
        del sys.modules[module_name]


yo = load_yo()


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


class TestGuestHelperCheck(unittest.TestCase):
    def failed(self, stdout, stderr):
        return subprocess.CompletedProcess(["ssh"], 3, stdout, stderr)

    def test_a_failure_with_only_binary_stdout_is_a_yo_error_with_that_text(self):
        # agent_paths runs the helper with binary=True: stdout is bytes there.
        with self.assertRaises(yo.YoError) as caught:
            yo.guest.guest_helper_check(self.failed(b"find: odd\n", ""), "projects")
        self.assertEqual(
            caught.exception.message, "projects failed (exit 3): find: odd"
        )

    def test_a_failure_that_said_nothing_is_a_yo_error_that_says_so(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.guest.guest_helper_check(self.failed(b"", ""), "projects")
        self.assertEqual(
            caught.exception.message, "projects failed (exit 3) with no output"
        )

    def test_stderr_comes_before_stdout(self):
        with self.assertRaisesRegex(yo.YoError, "exit 3.: boom"):
            yo.guest.guest_helper_check(self.failed("out", "boom\n"), "x")


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


class TestSshRunArgv(FakeHome):
    def capture_argv(self, role=None, **kwargs):
        seen = {}

        def fake_run(argv, **rest):
            seen["argv"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.proc, "run", fake_run):
            yo.ssh_run(role or yo.OPERATOR, ["true"], **kwargs)
        return seen["argv"]

    def test_no_stdin_closes_it_with_dash_n(self):
        self.assertIn("-n", self.capture_argv())

    def test_piped_stdin_keeps_it_open(self):
        self.assertNotIn("-n", self.capture_argv(stdin=b"script"))

    def test_agent_role_carries_user_and_control_path(self):
        argv = yo.ssh_options(yo.AGENT, mux=True)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=%s/.lima/yolobox/ssh-agent.sock" % self.home, argv)

    def test_a_home_too_long_for_the_ssh_master_socket_is_named_before_ssh_runs(self):
        # ssh would exit 255 on it, which yo reports as a box it cannot reach.
        # ssh binds the master at "<ControlPath>.<16 random characters>", 17
        # bytes more than the ControlPath, so that is the length the limit
        # applies to (measured against OpenSSH: a 87-byte path fails on macOS).
        bound = len(os.fsencode(yo.state.agent_control_path())) + 17
        with mock.patch.object(yo.state, "UNIX_SOCKET_PATH_LIMIT", bound + 1):
            yo.ssh_options(yo.AGENT, mux=True)
        with mock.patch.object(yo.state, "UNIX_SOCKET_PATH_LIMIT", bound):
            with self.assertRaises(yo.YoError) as caught:
                yo.ssh_options(yo.OPERATOR)
        self.assertIn("must be shorter than %d" % bound, caught.exception.message)
        self.assertIn(self.home, caught.exception.message)

    def test_a_real_overlong_home_is_refused(self):
        with with_home("/" + "d" * yo.state.UNIX_SOCKET_PATH_LIMIT):
            with self.assertRaisesRegex(yo.YoError, "unix socket path must be shorter"):
                yo.ssh_options(yo.OPERATOR)


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
            return mock.Mock(returncode=0, stdout=b"", stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
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

    def test_guest_source_globs_are_escaped_for_scp(self):
        argv = self.run_cp([":~/a[1]*?\\.txt", "out"])["run"]
        self.assertIn("lima-yolobox:/home/agent/a\\[1\\]\\*\\?\\\\.txt", argv)

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
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", "a.txt", ":~/x"]))

        steps = [
            "mkdir" if argv[:2] == ["mkdir", "-p"] else kind
            for kind, argv in events
            if argv != ["true"]
        ]
        self.assertEqual(steps, ["mkdir", "run"])
        self.assertEqual(events[-1][1][0], "scp")

    def test_no_mkdir_for_from_guest(self):
        calls = self.run_cp([":~/x", "dest.txt"])
        # The agent probe, then the one find that lists the source: no mkdir,
        # and nothing else is run in the guest.
        self.assertEqual([argv[0] for argv in calls["ssh_run"]], ["true", "find"])
        self.assertIn("lima-yolobox:/home/agent/x", calls["run"])


class FakeGuest:
    """The agent's home in the guest, answering the two commands cmd_cp sends it:
    find over ssh and scp. Like the real scp it takes a source whole, rejects
    glob characters unless they are escaped, and leaves a directory with its
    mode plus u+wx."""

    HOME = "/home/agent"
    PRUNED = ("node_modules", ".venv")
    LISTED = ("target", ".next")
    MANIFESTS = ("Cargo.toml", "package.json")

    def __init__(self, files, modes, scp_sources, scp_destinations, scp_fails):
        self.files = {self.HOME + "/" + name: text for name, text in files.items()}
        self.modes = {self.HOME + "/" + name: mode for name, mode in modes.items()}
        self.scp_sources = scp_sources
        self.scp_destinations = scp_destinations
        self.scp_fails = scp_fails
        self.received = []  # each scp call's source operands, as scp gets them
        self.destination_modes = []  # the destination's mode when scp is called

    def is_dir(self, path):
        return any(name.startswith(path + "/") for name in self.files)

    def exists(self, path):
        return path in self.files or self.is_dir(path)

    def children(self, path):
        prefix = path + "/"
        return sorted(
            {
                prefix + name[len(prefix) :].split("/")[0]
                for name in self.files
                if name.startswith(prefix)
            }
        )

    def record(self, path):
        kind = "d" if self.is_dir(path) else "p" if self.files[path] is None else "f"
        mode = self.modes.get(path, 0o755 if kind == "d" else 0o644)
        return os.fsencode("%s\t%o\t1700000000.5\t%s\0" % (kind, mode, path))

    def below(self, start):
        # find's walk under `start`, printing what the build-output expression
        # asks for and not entering what it prunes.
        for path in self.children(start):
            name = path.rsplit("/", 1)[1]
            is_dir = self.is_dir(path)
            if is_dir and name in self.PRUNED:
                yield path
                continue
            if (is_dir and name in self.LISTED) or (
                not is_dir and name in self.MANIFESTS
            ):
                yield path
            if is_dir:
                yield from self.below(path)

    def ssh_run(self, role, argv, **kwargs):
        done = mock.Mock(returncode=0, stdout=b"", stderr="")
        if argv[0] != "find":
            return done
        assert argv[1] == "-H", argv
        assert argv[argv.index("-printf") + 1] == "%y\t%m\t%T@\t%p\\0", argv
        options = next(i for i, arg in enumerate(argv) if i > 1 and arg.startswith("-"))
        for start in argv[2:options]:
            if not self.exists(start):
                done.returncode = 1
                done.stderr = "find: %s: No such file or directory" % start
                return done
            if "-maxdepth" in argv:
                found = [start] + self.children(start)
            else:
                found = list(self.below(start))
            done.stdout += b"".join(self.record(path) for path in found)
        return done

    def unescape(self, source):
        host, _, escaped = source.partition(":")
        assert host == "lima-yolobox", source
        path, chars = "", iter(escaped)
        for char in chars:
            assert char not in "*?[", "unescaped glob character in %r" % source
            path += next(chars) if char == "\\" else char
        return path

    def copy_into(self, path, directory):
        target = os.path.join(directory, path.rsplit("/", 1)[1])
        if path in self.files:
            if self.files[path] is not None:  # None is a fifo, which scp -r passes over
                Path(target).write_text(self.files[path])
            return
        os.makedirs(target, exist_ok=True)
        for child in self.children(path):
            self.copy_into(child, target)
        # sftp-mode scp, like OpenSSH's download_dir: mkdir(mode | S_IRWXU).
        os.chmod(target, self.modes.get(path, 0o755) | stat.S_IRWXU)
        os.utime(target, (1700000000, 1700000000))

    def scp(self, argv, **kwargs):
        separator = argv.index("--")
        sources = [self.unescape(source) for source in argv[separator + 1 : -1]]
        self.scp_sources.append(sources)
        self.scp_destinations.append(argv[-1])
        self.received.append(argv[separator + 1 : -1])
        self.destination_modes.append(stat.S_IMODE(os.stat(argv[-1]).st_mode))
        if self.scp_fails:
            raise yo.YoError("scp failed")
        for path in sources:
            if not self.exists(path):
                raise yo.YoError("scp: %s: No such file or directory" % path)
            self.copy_into(path, argv[-1])
        return mock.Mock(returncode=0, stdout="", stderr="")


class CpFromGuestHarness(CpHarness):
    """The guest is a FakeGuest: cmd_cp lists it with find and pulls from it with
    scp, and the files land in the download directory like the real ones would."""

    def setUp(self):
        super().setUp()
        self.scp_destinations = []
        self.scp_sources = []

    def copy(self, paths, guest_files, scp_fails=False, guest_modes=None):
        # guest_files: {path relative to the guest home: content}
        # guest_modes: {directory relative to the guest home: mode}
        guest = FakeGuest(
            guest_files,
            guest_modes or {},
            self.scp_sources,
            self.scp_destinations,
            scp_fails,
        )
        self.guest = guest
        with (
            mock.patch.object(yo.proc, "run", guest.scp),
            mock.patch.object(yo.sshx, "ssh_run", guest.ssh_run),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp"] + paths))

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def leftovers(self, directory):
        return [n for n in os.listdir(directory) if n.startswith(".yo-cp-")]

    def unprefixed_lines(self, reported):
        """The lines of a report that do not start a `yo: ` message: a hostile
        name that forged one would show up here."""
        return [line for line in reported.splitlines() if not line.startswith("yo: ")]

    def pull(self, guest_files, source=":~/proj", guest_modes=None):
        stderr = io.StringIO()
        with mock.patch.object(
            yo.ui, "err", side_effect=lambda line="": stderr.write(line + "\n")
        ):
            self.copy([source, "dest/"], guest_files, guest_modes=guest_modes)
        return stderr.getvalue()

    def landed(self):
        found = []
        for directory, _, files in os.walk("dest"):
            found += [os.path.join(directory, f) for f in files]
        return sorted(found)


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
        self.assertEqual(self.scp_destinations, [])

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
            os.path.dirname(self.scp_destinations[0]), os.path.join(self.cwd, "dest")
        )
        self.assertEqual(os.path.dirname(self.scp_destinations[1]), self.cwd)

    def test_staging_is_removed_when_scp_fails(self):
        os.makedirs("dest")

        with self.assertRaisesRegex(yo.YoError, "scp failed"):
            self.copy([":~/x", "dest/"], {"x": "data"}, scp_fails=True)

        self.assertEqual(os.listdir("dest"), [])

    def test_a_read_only_guest_directory_is_still_cleaned_up_on_refusal(self):
        os.makedirs("dest/proj")
        os.symlink("/etc/hosts", "dest/proj/b")

        def fake_run(argv, **kwargs):
            tree = os.path.join(argv[-1], "proj")
            os.makedirs(tree)
            Path(tree, "b").write_text("x")
            os.chmod(tree, 0o555)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(
                yo.sshx,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout=b"", stderr=""),
            ),
            self.assertRaisesRegex(yo.YoError, "refusing to copy over symlink"),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", ":~/proj", "dest/"]))

        self.assertEqual(self.leftovers("dest"), [])

    def scp_creating(self, relative, mode):
        # A fake scp that leaves one directory with the given mode, as sftp-mode
        # scp does for a guest directory the agent chmodded.
        def fake_run(argv, **kwargs):
            tree = os.path.join(argv[-1], relative)
            os.makedirs(tree)
            os.chmod(tree, mode)
            return mock.Mock(returncode=0, stdout="", stderr="")

        return fake_run

    def run_cp_with(self, fake_run, paths):
        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(
                yo.sshx,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout=b"", stderr=""),
            ),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp"] + paths))

    def test_an_unreadable_staged_subdirectory_is_cleaned_up(self):
        os.makedirs("dest")
        make_dir = self.scp_creating("proj/sub", 0o300)

        def fake_run(argv, **kwargs):
            make_dir(argv)
            raise yo.YoError("scp failed")

        with self.assertRaises(yo.YoError) as caught:
            self.run_cp_with(fake_run, [":~/proj", "dest/"])

        self.assertEqual(caught.exception.message, "scp failed")
        self.assertEqual(self.leftovers("dest"), [])

    def test_an_unreadable_staged_subdirectory_does_not_break_a_good_copy(self):
        os.makedirs("dest")

        self.addCleanup(os.chmod, os.path.join(self.cwd, "dest/proj/sub"), 0o700)

        self.run_cp_with(self.scp_creating("proj/sub", 0o300), [":~/proj", "dest/"])

        self.assertEqual(self.leftovers("dest"), [])
        self.assertTrue(os.path.isdir("dest/proj/sub"))

    def test_a_cleanup_failure_is_reported_next_to_the_original_error(self):
        os.makedirs("dest")
        stderr = io.StringIO()

        def fake_run(argv, **kwargs):
            raise yo.YoError("scp failed")

        with (
            mock.patch.object(yo.cp, "remove_staging", side_effect=yo.YoError("stuck")),
            mock.patch("sys.stderr", stderr),
            self.assertRaises(yo.YoError) as caught,
        ):
            self.run_cp_with(fake_run, [":~/proj", "dest/"])

        self.assertEqual(caught.exception.message, "scp failed")
        self.assertIn("stuck", stderr.getvalue())

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
        self.assertEqual(self.scp_destinations, [])
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

    def test_every_version_control_editor_and_direnv_name_is_skipped(self):
        for name in (".hg", ".svn", ".idea", ".ENVRC", ".Vscode"):
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

    def test_a_control_path_under_an_unreadable_staged_directory_is_removed(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/sub/.git/config": "evil", "proj/sub/ok": "ok"},
            guest_modes={"proj/sub": 0o300},
        )
        self.assertEqual(self.landed(), ["dest/proj/sub/ok"])
        self.assertIn('skipped "proj/sub/.git"', reported)
        self.assertEqual(self.leftovers("dest"), [])

    def test_directory_modes_scp_kept_survive_the_landing(self):
        os.makedirs("dest")
        self.pull(
            {"proj/pub/x": "1", "proj/priv/x": "2"},
            guest_modes={"proj/pub": 0o755, "proj/priv": 0o700},
        )
        for name, mode in (("pub", 0o755), ("priv", 0o700)):
            landed_mode = stat.S_IMODE(os.stat("dest/proj/%s" % name).st_mode)
            self.assertEqual(landed_mode, mode, name)


class TestCpFromGuestNeverLandsBuildOutput(CpFromGuestHarness):
    """node_modules, .venv, Rust target and Next.js .next dwarf the source and
    the project's tooling recreates them, so a pulled tree does not carry them."""

    def skipped_lines(self, reported):
        return [line for line in reported.splitlines() if "skipped" in line]

    def test_dependency_installs_are_skipped_and_reported_once_each(self):
        os.makedirs("dest")
        reported = self.pull(
            {
                "proj/node_modules/x/index.js": "js",
                "proj/.venv/bin/python": "py",
                "proj/src/a.py": "a",
            }
        )
        self.assertEqual(self.landed(), ["dest/proj/src/a.py"])
        for skipped in ("node_modules", ".venv"):
            self.assertEqual(reported.count('skipped "proj/%s"' % skipped), 1)
        self.assertEqual(len(self.skipped_lines(reported)), 2)
        self.assertEqual(self.leftovers("dest"), [])

    def test_rust_target_beside_its_cargo_toml_is_skipped(self):
        os.makedirs("dest")
        reported = self.pull({"proj/target/debug/bin": "elf", "proj/Cargo.toml": "c"})
        self.assertEqual(self.landed(), ["dest/proj/Cargo.toml"])
        self.assertIn('skipped "proj/target"', reported)

    def test_a_target_without_cargo_toml_is_a_data_directory_and_lands(self):
        os.makedirs("dest")
        reported = self.pull({"proj/target/data.csv": "1,2"})
        self.assertEqual(self.landed(), ["dest/proj/target/data.csv"])
        self.assertEqual(self.skipped_lines(reported), [])

    def test_next_output_beside_its_package_json_is_skipped(self):
        os.makedirs("dest")
        reported = self.pull({"proj/.next/x": "built", "proj/package.json": "{}"})
        self.assertEqual(self.landed(), ["dest/proj/package.json"])
        self.assertIn('skipped "proj/.next"', reported)

    def test_a_dot_next_without_package_json_lands(self):
        os.makedirs("dest")
        reported = self.pull({"proj/.next/x": "data"})
        self.assertEqual(self.landed(), ["dest/proj/.next/x"])
        self.assertEqual(self.skipped_lines(reported), [])

    def test_a_manifest_that_is_a_directory_does_not_count(self):
        os.makedirs("dest")
        reported = self.pull({"proj/Cargo.toml/x": "1", "proj/target/y": "2"})
        self.assertEqual(
            self.landed(), ["dest/proj/Cargo.toml/x", "dest/proj/target/y"]
        )
        self.assertEqual(self.skipped_lines(reported), [])

    def test_build_output_nested_in_the_tree_is_skipped(self):
        os.makedirs("dest")
        reported = self.pull({"proj/web/node_modules/a": "dep", "proj/web/src/b": "b"})
        self.assertEqual(self.landed(), ["dest/proj/web/src/b"])
        self.assertIn('skipped "proj/web/node_modules"', reported)

    def test_a_source_the_operator_named_lands_even_when_it_is_build_output(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/node_modules/a": "x"}, source=":~/proj/node_modules"
        )
        self.assertEqual(self.landed(), ["dest/node_modules/a"])
        self.assertEqual(self.skipped_lines(reported), [])

    def test_a_file_called_node_modules_lands(self):
        os.makedirs("dest")
        reported = self.pull({"proj/node_modules": "not a directory"})
        self.assertEqual(self.landed(), ["dest/proj/node_modules"])
        self.assertEqual(self.skipped_lines(reported), [])

    def test_a_control_path_inside_build_output_is_reported_only_as_build_output(
        self,
    ):
        os.makedirs("dest")
        reported = self.pull({"proj/node_modules/.git/config": "evil", "proj/ok": "1"})
        self.assertEqual(self.landed(), ["dest/proj/ok"])
        self.assertEqual(
            self.skipped_lines(reported),
            ['yo: skipped "proj/node_modules": ' + yo.BUILD_OUTPUT_NOTE],
        )

    def test_build_output_already_on_the_host_is_left_untouched(self):
        os.makedirs("dest/proj/node_modules")
        Path("dest/proj/node_modules/keep").write_text("mine")
        self.pull({"proj/node_modules/new": "agent", "proj/ok": "1"})
        self.assertEqual(self.read("dest/proj/node_modules/keep"), "mine")
        self.assertFalse(os.path.lexists("dest/proj/node_modules/new"))
        self.assertEqual(self.read("dest/proj/ok"), "1")


class TestCpFromGuestKeepsDirectoryModes(CpFromGuestHarness):
    """A directory made beside build output gets the mode OpenSSH's sftp client
    gives one: the guest's mode with the owner's rwx added."""

    def setUp(self):
        super().setUp()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        os.makedirs("dest")

    def test_the_owner_can_read_write_and_enter_the_directory_scp_fills(self):
        self.pull(
            {"proj/sub/node_modules/x": "dep", "proj/sub/a": "1"},
            guest_modes={"proj/sub": 0o100},
        )
        # the directory the children are fetched into, while scp writes there
        self.assertEqual(self.guest.destination_modes[-1], 0o700)

    def test_a_read_only_directory_lands_with_the_group_and_other_bits_it_had(self):
        for guest_files in (
            {"proj/sub/a": "1"},
            {"proj/sub/node_modules/x": "dep", "proj/sub/a": "1"},
        ):
            with self.subTest(files=sorted(guest_files)):
                shutil.rmtree("dest")
                os.makedirs("dest")
                self.pull(guest_files, guest_modes={"proj/sub": 0o555})
                self.assertEqual(stat.S_IMODE(os.stat("dest/proj/sub").st_mode), 0o755)


class TestCpFromGuestNeverFetchesBuildOutput(CpFromGuestHarness):
    """Build output is left out on the guest, so its bytes never cross the wire."""

    def requested(self):
        return sorted(path for call in self.scp_sources for path in call)

    def test_no_build_output_byte_is_asked_of_scp(self):
        os.makedirs("dest")
        self.pull(
            {
                "proj/node_modules/x/index.js": "js",
                "proj/web/node_modules/y": "js",
                "proj/web/.venv/bin/python": "py",
                "proj/web/src/a.py": "a",
                "proj/target/debug/bin": "elf",
                "proj/Cargo.toml": "c",
                "proj/README": "r",
            }
        )
        self.assertEqual(
            self.requested(),
            [
                "/home/agent/proj/Cargo.toml",
                "/home/agent/proj/README",
                "/home/agent/proj/web/src",
            ],
        )
        self.assertEqual(
            self.landed(),
            [
                "dest/proj/Cargo.toml",
                "dest/proj/README",
                "dest/proj/web/src/a.py",
            ],
        )

    def test_a_tree_without_build_output_is_one_whole_transfer(self):
        os.makedirs("dest")
        self.pull({"proj/a": "1", "proj/sub/b": "2"})
        self.assertEqual(self.scp_sources, [["/home/agent/proj"]])

    def test_a_nested_target_that_proves_nothing_is_still_searched(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/target/data.csv": "1,2", "proj/target/node_modules/z": "dep"}
        )
        self.assertEqual(self.landed(), ["dest/proj/target/data.csv"])
        self.assertIn('skipped "proj/target/node_modules"', reported)
        self.assertEqual(self.requested(), ["/home/agent/proj/target/data.csv"])

    def test_build_output_inside_build_output_is_reported_once(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/target/debug/node_modules/x": "dep", "proj/Cargo.toml": "c"}
        )
        self.assertEqual(self.landed(), ["dest/proj/Cargo.toml"])
        self.assertEqual(reported.count("skipped"), 1)
        self.assertIn('skipped "proj/target"', reported)

    def test_a_fifo_beside_build_output_is_passed_over_with_a_note(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/node_modules/x": "dep", "proj/pipe": None, "proj/a": "1"}
        )
        self.assertEqual(self.landed(), ["dest/proj/a"])
        self.assertIn('skipped "proj/pipe": not a regular file', reported)
        self.assertEqual(self.requested(), ["/home/agent/proj/a"])

    def test_hostile_names_in_skip_reports_are_escaped(self):
        os.makedirs("dest")
        reported = self.pull(
            {
                "proj/a\x1b[2J\nb/node_modules/x": "dep",
                "proj/p\rq": None,
                "proj/node_modules/y": "dep",
            }
        )
        self.assertEqual(self.unprefixed_lines(reported), [])
        self.assertNotIn("\x1b", reported)
        self.assertNotIn("\r", reported)
        self.assertIn('skipped "proj/a\\x1b[2J\\nb/node_modules"', reported)
        self.assertIn('skipped "proj/p\\rq": not a regular file', reported)

    def test_the_skip_is_reported_relative_to_the_named_source(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/web/node_modules/a": "x", "proj/web/b": "1"}, ":~/proj/web"
        )
        self.assertIn('skipped "web/node_modules"', reported)
        self.assertEqual(self.landed(), ["dest/web/b"])

    def test_every_source_is_judged_and_the_others_still_go_whole(self):
        os.makedirs("dest")
        self.copy(
            [":~/a", ":~/b", "dest/"],
            {"a/node_modules/x": "dep", "a/keep": "1", "b/f": "2"},
        )
        self.assertEqual(self.landed(), ["dest/a/keep", "dest/b/f"])
        self.assertEqual(self.requested(), ["/home/agent/a/keep", "/home/agent/b"])

    def test_names_with_spaces_dashes_newlines_and_wildcards_travel_intact(self):
        os.makedirs("dest")
        names = ["a b.txt", "-rf", "line\nbreak", "w[1]*?\\.txt", "sp ace/in"]
        files = {"proj/%s" % name: name for name in names}
        files["proj/node_modules/x"] = "dep"
        self.pull(files)
        for name in names:
            self.assertEqual(self.read("dest/proj/%s" % name), name)
        self.assertFalse(os.path.exists("dest/proj/node_modules"))

    def test_a_directory_beside_build_output_keeps_the_mode_and_time_scp_gave_it(self):
        os.makedirs("dest")
        self.pull(
            {"proj/node_modules/x": "dep", "proj/a": "1"},
            guest_modes={"proj": 0o750},
        )
        status = os.stat("dest/proj")
        self.assertEqual(stat.S_IMODE(status.st_mode), 0o750)
        self.assertEqual(status.st_mtime, 1700000000.5)

    def test_a_read_only_directory_beside_build_output_is_made_writable_like_scp_does(
        self,
    ):
        os.makedirs("dest")
        self.pull(
            {"proj/sub/node_modules/x": "dep", "proj/sub/a": "1"},
            guest_modes={"proj/sub": 0o500},
        )
        self.assertEqual(stat.S_IMODE(os.stat("dest/proj/sub").st_mode), 0o700)
        self.assertEqual(self.read("dest/proj/sub/a"), "1")

    def test_a_directory_of_thousands_of_entries_does_not_overflow_the_command_line(
        self,
    ):
        os.makedirs("dest")
        names = ["%04d-%s" % (n, "x" * 90) for n in range(2000)]
        files = {"proj/%s" % name: "1" for name in names}
        files["proj/node_modules/x"] = "dep"
        self.pull(files)
        self.assertEqual(len(self.landed()), 2000)
        for call in self.scp_sources:
            self.assertLess(sum(len(path) + 1 for path in call), 100000)
        self.assertGreater(len(self.scp_sources), 1)

    def test_batches_are_measured_as_scp_receives_them_with_host_and_escapes(self):
        os.makedirs("dest")
        # Every character is a wildcard, so the escaped spelling is twice the path.
        names = ["%03d-%s" % (n, "[" * 150) for n in range(400)]
        files = {"proj/%s" % name: "1" for name in names}
        files["proj/node_modules/x"] = "dep"
        self.pull(files)
        self.assertEqual(len(self.landed()), 400)
        received = [sum(len(arg) + 1 for arg in call) for call in self.guest.received]
        self.assertGreater(len(received), 1)
        self.assertLessEqual(max(received), 65536)

    def test_a_source_the_guest_does_not_have_is_an_error_naming_it(self):
        os.makedirs("dest")
        with self.assertRaisesRegex(
            yo.YoError, "No such file.*proj|proj.*No such file"
        ):
            self.pull({"other/a": "1"})
        self.assertEqual(self.scp_sources, [])
        self.assertEqual(self.leftovers("dest"), [])

    def test_a_control_path_beside_build_output_is_still_dropped(self):
        os.makedirs("dest")
        reported = self.pull(
            {"proj/node_modules/x": "dep", "proj/.git/config": "evil", "proj/a": "1"}
        )
        self.assertEqual(self.landed(), ["dest/proj/a"])
        self.assertIn('skipped "proj/.git"', reported)
        self.assertIn('skipped "proj/node_modules"', reported)


class TestCpFromGuestDistrustsTheListing(CpFromGuestHarness):
    """The listing is whatever the agent's login shell prints, so every record
    is untrusted: one that is not a clean path under a named source, or is
    malformed, or contradicts the second listing, is an error and nothing is
    fetched on its strength."""

    FILES = {"proj/node_modules/x": "dep", "proj/a/b": "1", "z/node_modules/q": "d"}

    def pull_listing_through(self, forge, files=None):
        # forge(argv, stdout) -> stdout, applied to every find the guest answers.
        os.makedirs("dest", exist_ok=True)
        guest = FakeGuest(
            files or self.FILES, {}, self.scp_sources, self.scp_destinations, False
        )
        real_ssh_run = guest.ssh_run

        def forged(role, argv, **kwargs):
            done = real_ssh_run(role, argv, **kwargs)
            if argv[0] == "find":
                done.stdout = forge(argv, done.stdout)
            return done

        with (
            mock.patch.object(yo.proc, "run", guest.scp),
            mock.patch.object(yo.sshx, "ssh_run", forged),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", ":~/proj", "dest/"]))

    def refused(self, forge, message, files=None):
        with self.assertRaisesRegex(yo.YoError, message):
            self.pull_listing_through(forge, files)
        self.assertEqual(self.scp_sources, [])
        self.assertEqual(os.listdir("dest"), [])

    @staticmethod
    def first_listing_adds(record):
        def forge(argv, stdout):
            return stdout if "-maxdepth" in argv else stdout + record

        return forge

    @staticmethod
    def record(path, kind="d", mode="755", mtime="1.0"):
        return os.fsencode("%s\t%s\t%s\t%s\0" % (kind, mode, mtime, path))

    def test_a_record_that_is_not_a_clean_path_under_a_source_is_refused(self):
        for forged in (
            "/home/agent/proj/a/../../../z/node_modules",
            "/home/agent/proj/../z/node_modules",
            "/etc/node_modules",
            "/home/agent/z/node_modules",
            "/home/agent/projx/node_modules",
            "/home/agent/proj//node_modules",
            "/home/agent/proj/./node_modules",
            "/home/agent/proj/node_modules/",
            "/home/agent/proj",
            "proj/node_modules",
            "",
        ):
            with self.subTest(forged=forged):
                self.refused(
                    self.first_listing_adds(self.record(forged)), "the guest's listing"
                )

    def test_a_forged_path_names_the_record_without_letting_escapes_through(self):
        forged = "/home/agent/proj/\x1b[31m../../x/node_modules"
        with self.assertRaises(yo.YoError) as caught:
            self.pull_listing_through(self.first_listing_adds(self.record(forged)))
        self.assertIn(repr("d\t755\t1.0\t" + forged), caught.exception.message)
        self.assertNotIn("\x1b", caught.exception.message)

    def test_a_forged_listing_cannot_make_scp_leave_staging(self):
        # Used to land /home/agent/proj/a/../../../.envrc in the download.
        with self.assertRaises(yo.YoError):
            self.pull_listing_through(
                self.first_listing_adds(
                    self.record("/home/agent/proj/a/../../../z/node_modules")
                )
            )
        self.assertEqual(
            [source for call in self.scp_sources for source in call if ".." in source],
            [],
        )

    def test_a_malformed_record_is_refused_and_named(self):
        for malformed in (
            "d\t755\t1.0",
            "d\t755",
            "garbage",
            "d\t999\t1.0\t/home/agent/proj/node_modules",
            "d\t0o755\t1.0\t/home/agent/proj/node_modules",
            "d\t-755\t1.0\t/home/agent/proj/node_modules",
            "d\t755\tsoon\t/home/agent/proj/node_modules",
            "d\t755\tnan\t/home/agent/proj/node_modules",
            "d\t755\tinf\t/home/agent/proj/node_modules",
            "d\t755\t1_0\t/home/agent/proj/node_modules",
            "d\t755\t99999999999999999999\t/home/agent/proj/node_modules",
            "dd\t755\t1.0\t/home/agent/proj/node_modules",
            "\t755\t1.0\t/home/agent/proj/node_modules",
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(yo.YoError) as caught:
                    self.pull_listing_through(
                        self.first_listing_adds(os.fsencode(malformed) + b"\0")
                    )
                self.assertIn(repr(malformed), caught.exception.message)

    def test_two_records_that_disagree_about_one_path_are_refused(self):
        self.refused(
            # the guest's own record of node_modules says 755
            self.first_listing_adds(
                self.record("/home/agent/proj/node_modules", mode="700")
            ),
            "the guest's listing",
        )

    def test_a_record_for_a_name_find_was_never_asked_for_is_refused(self):
        self.refused(
            self.first_listing_adds(self.record("/home/agent/proj/a")),
            "the guest's listing",
        )

    def test_a_directory_listed_twice_by_overlapping_sources_is_not_a_forgery(self):
        os.makedirs("dest")
        guest = FakeGuest(
            self.FILES, {}, self.scp_sources, self.scp_destinations, False
        )
        with (
            mock.patch.object(yo.proc, "run", guest.scp),
            mock.patch.object(yo.sshx, "ssh_run", guest.ssh_run),
        ):
            args = ["cp", ":~/proj", ":~/proj/a", "dest/"]
            yo.cmd_cp(yo.build_parser().parse_args(args))
        self.assertEqual(self.read("dest/a/b"), "1")
        self.assertEqual(self.read("dest/proj/a/b"), "1")

    def test_a_start_missing_from_the_second_listing_is_an_error_not_a_traceback(self):
        def forge(argv, stdout):
            if "-maxdepth" not in argv:
                return stdout
            return b"".join(
                r + b"\0"
                for r in stdout.split(b"\0")
                if r and b"\t/home/agent/proj\0" not in r + b"\0"
            )

        self.refused(forge, "changed")

    def test_a_directory_that_stopped_being_one_is_an_error_not_a_traceback(self):
        def forge(argv, stdout):
            if "-maxdepth" not in argv:
                return stdout
            return stdout.replace(
                b"d\t755\t1700000000.5\t/home/agent/proj\0",
                b"f\t644\t1700000000.5\t/home/agent/proj\0",
            )

        self.refused(forge, "changed")

    def test_a_walked_directory_emptied_between_the_listings_is_an_error(self):
        # npm removing node_modules between the two finds.
        def forge(argv, stdout):
            if "-maxdepth" not in argv:
                return stdout
            return stdout.split(b"\0")[0] + b"\0"

        self.refused(forge, "changed")


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
            return mock.Mock(returncode=0, stdout=b"", stderr="")

        with (
            mock.patch.object(yo.proc, "run", fake_run),
            mock.patch.object(yo.sshx, "ssh_run", fake_ssh_run),
            self.assertRaisesRegex(yo.YoError, "refusing to upload symlink"),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp", "a.txt", "link", ":~/x"]))
        self.assertEqual(calls, {"run": [], "ssh_run": [["true"]]})

    def test_a_trailing_slash_does_not_hide_a_symlink_source(self):
        os.makedirs("real")
        Path("real/secret").write_text("x")
        os.symlink("real", "lnk")
        self.refused(["lnk/", ":~/x"], os.path.join(self.cwd, "lnk"))

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

    def test_a_guest_source_named_like_a_host_symlink_is_not_checked_here(self):
        # The upload check walks host paths. A guest operand is only ever handed
        # to scp, so a host symlink with the same name is not a reason to refuse.
        os.symlink("a.txt", "link")
        argv = self.run_cp([":link", "out"])["run"]
        self.assertIn("lima-yolobox:/home/agent/link", argv)
        self.assertTrue(os.path.islink("link"))


class TestHome(unittest.TestCase):
    def test_home_is_normalised_so_it_equals_a_path_built_from_it(self):
        for spelled in ("/Users/x/", "/Users/x//", "/Users/./x"):
            with self.subTest(home=spelled), with_home(spelled):
                self.assertEqual(yo.home(), "/Users/x")


@unittest.skipIf(shutil.which("ssh") is None, "no ssh")
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
                'Host %s\n  UserKnownHostsFile "%s"\n'
                % (yo.box.vm_host, yo.known_hosts_path())
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

LIMA_BLOCK_MISSING_ALIAS_FIELDS = (
    "Host lima-yolobox\n  ControlPath ~/.lima/yolobox/ssh.sock\n  User agent\n"
)


class TestEnsureAgentSshConfig(FakeHome):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(yo.ui, "err")
        self.err = patcher.start()
        self.addCleanup(patcher.stop)

    def config_path(self):
        ssh_dir = os.path.join(self.home, ".lima", "yolobox")
        os.makedirs(ssh_dir, exist_ok=True)
        return os.path.join(ssh_dir, "ssh.config")

    def write_config(self, text):
        path = self.config_path()
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def read_config(self):
        with open(self.config_path(), "r") as handle:
            return handle.read()

    def test_missing_file_is_not_an_error(self):
        yo.ensure_agent_ssh_config()
        self.err.assert_not_called()

    def test_yolobox_block_precedes_limas_own_control_path(self):
        path = self.write_config(LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertLess(content.index("ssh-agent.sock"), content.index("ssh.sock"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_agent_match_block_forwards_no_agent(self):
        self.write_config(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()
        match = self.read_config().split("Match host", 1)[1].split("\n\n", 1)[0]
        self.assertIn("user agent", match)
        self.assertIn("\n  ForwardAgent no", match)

    def test_second_call_is_byte_identical(self):
        self.write_config(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()
        first = self.read_config()

        yo.ensure_agent_ssh_config()

        self.assertEqual(self.read_config(), first)

    def test_lima_regenerated_file_is_repaired_without_duplicating(self):
        path = self.write_config(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()

        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertEqual(content.count("ssh-agent.sock"), 1)
        self.assertLess(content.index("ssh-agent.sock"), content.index("ssh.sock"))

    def test_herdr_alias_is_present_as_agent_with_no_shared_control_path(self):
        self.write_config(LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertIn("Host yolobox\n", content)
        alias = content.split("Host yolobox\n", 1)[1]
        self.assertIn("  User agent\n", alias)
        self.assertIn("  ControlPath none\n", alias)
        self.assertIn("  ForwardAgent no\n", alias)

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_no_user_forwards_even_if_a_later_host_block_says_yes(self):
        # A user's own "Host lima-yolobox / ForwardAgent yes" sits after the
        # Include; ssh keeps the first value, so the owned blocks must win, for
        # the operator too: only `yo ssh` forwards, by its own command line.
        self.write_config(LIMA_BLOCK + "\nHost lima-yolobox\n  ForwardAgent yes\n")
        yo.ensure_agent_ssh_config()

        def forwardagent(user):
            proc = subprocess.run(
                ["ssh", "-G", "-F", self.config_path(), "-o", "User=" + user]
                + ["lima-yolobox"],
                capture_output=True,
                text=True,
                check=True,
            )
            return [line for line in proc.stdout.splitlines() if "forwardagent" in line]

        self.assertEqual(forwardagent("agent"), ["forwardagent no"])
        self.assertEqual(forwardagent("user"), ["forwardagent no"])

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_every_connection_gives_up_on_a_guest_that_stops_answering(self):
        # Whatever lima's block or a later user block says, as ssh resolves it.
        later = "  ServerAliveInterval 0\n  ConnectTimeout 0\n"
        self.write_config(
            LIMA_BLOCK + "\nHost lima-yolobox\n" + later + "\nHost yolobox\n" + later
        )
        yo.ensure_agent_ssh_config()
        # yo's roles as either account, and herdr through its alias.
        for user, host in (
            ("agent", "lima-yolobox"),
            ("user", "lima-yolobox"),
            ("agent", "yolobox"),
        ):
            with self.subTest(user=user, host=host):
                resolved = subprocess.run(
                    ["ssh", "-G", "-F", self.config_path(), "-o", "User=" + user]
                    + [host],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.splitlines()
                self.assertIn("connecttimeout 30", resolved)
                self.assertIn("serveraliveinterval 15", resolved)
                self.assertIn("serveralivecountmax 8", resolved)

    def test_a_marker_without_a_blank_line_keeps_limas_host_block(self):
        self.write_config(yo.AGENT_SSH_CONFIG_MARKER + "\n" + LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertIn(LIMA_BLOCK, content)
        self.assertIn("Host yolobox\n", content)
        first = content
        yo.ensure_agent_ssh_config()
        self.assertEqual(self.read_config(), first)

    def test_herdr_alias_takes_hostname_port_identityfile_from_lima(self):
        self.write_config(LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        alias = self.read_config().split("Host yolobox\n", 1)[1]
        self.assertIn("  Hostname 127.0.0.1\n", alias)
        self.assertIn("  Port 60022\n", alias)
        self.assertIn('  IdentityFile "/Users/user/.lima/_config/user"\n', alias)

    def test_herdr_alias_is_idempotent_across_reapplication(self):
        path = self.write_config(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()
        first = self.read_config()

        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertEqual(content, first)
        self.assertEqual(content.count("Host yolobox\n"), 1)

    def test_unparseable_lima_block_skips_alias_without_writing_broken_block(self):
        self.write_config(LIMA_BLOCK_MISSING_ALIAS_FIELDS)

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertNotIn("Host yolobox\n", content)
        self.err.assert_called_once()
        self.assertIn("yolobox", self.err.call_args[0][0])

    def test_missing_lima_block_entirely_skips_alias(self):
        self.write_config(
            "Host somewhere-else\n  Hostname 1.2.3.4\n  Port 22\n  IdentityFile x\n"
        )

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertNotIn("Host yolobox\n", content)
        self.err.assert_called_once()


LIMA_BLOCK_WITH_TWO_KEYS = (
    "Host lima-yolobox\n"
    '  IdentityFile "/Users/user/.lima/_config/user"\n'
    '  IdentityFile "/Users/user/.ssh/id_ed25519"\n'
    "  Hostname 127.0.0.1\n"
    "  Port 60022\n"
    "  Hostname 10.0.0.9\n"
    "  Port 1\n"
)


class TestParseLimaSshBlock(unittest.TestCase):
    def test_keeps_every_identity_file_in_order_and_the_first_other_value(self):
        block = yo.parse_ssh_host_block(LIMA_BLOCK_WITH_TWO_KEYS, "lima-yolobox")
        self.assertEqual(block.hostname, "127.0.0.1")
        self.assertEqual(block.port, "60022")
        self.assertEqual(
            block.identity_files,
            ("/Users/user/.lima/_config/user", "/Users/user/.ssh/id_ed25519"),
        )

    def test_the_herdr_alias_offers_every_identity_in_order(self):
        block = yo.parse_ssh_host_block(LIMA_BLOCK_WITH_TWO_KEYS, "lima-yolobox")
        lines = yo.herdr_alias_block(block).splitlines()
        self.assertEqual(
            [line for line in lines if "IdentityFile" in line],
            [
                '  IdentityFile "/Users/user/.lima/_config/user"',
                '  IdentityFile "/Users/user/.ssh/id_ed25519"',
            ],
        )


class TestHerdrAliasBlock(FakeHome):
    def block(self):
        return yo.herdr_alias_block(
            yo.SshHostBlock(
                "127.0.0.1", "60022", ("/Users/user/.lima/_config/user",), None
            )
        )

    def test_pins_the_yo_owned_known_hosts_file(self):
        self.assertIn('  UserKnownHostsFile "%s"' % yo.known_hosts_path(), self.block())

    def test_never_disables_strict_checking(self):
        block = self.block()
        self.assertNotIn("StrictHostKeyChecking no", block)
        self.assertNotIn("UserKnownHostsFile /dev/null", block)


class TestKnownHostsLines(unittest.TestCase):
    LIMA_VALUES = yo.SshHostBlock("127.0.0.1", "60022", ("x",), None)

    def test_formats_host_port_type_and_key(self):
        pubkeys = "ssh-ed25519 AAAAC3abc== root@yolobox\n"
        lines = yo.known_hosts_lines(self.LIMA_VALUES, pubkeys)
        self.assertEqual(lines, ["[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc=="])

    def test_handles_multiple_key_types_and_drops_the_comment(self):
        pubkeys = (
            "ssh-ed25519 AAAAC3abc== root@yolobox\n"
            "ecdsa-sha2-nistp256 AAAAE2abc== root@yolobox\n"
        )
        lines = yo.known_hosts_lines(self.LIMA_VALUES, pubkeys)
        self.assertEqual(
            lines,
            [
                "[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc==",
                "[127.0.0.1]:60022 ecdsa-sha2-nistp256 AAAAE2abc==",
            ],
        )

    def test_skips_blank_lines(self):
        pubkeys = "ssh-ed25519 AAAAC3abc== root@yolobox\n\n"
        lines = yo.known_hosts_lines(self.LIMA_VALUES, pubkeys)
        self.assertEqual(len(lines), 1)

    def test_ignores_a_line_with_no_key_field(self):
        pubkeys = "not-a-key-line\n"
        lines = yo.known_hosts_lines(self.LIMA_VALUES, pubkeys)
        self.assertEqual(lines, [])


LIMA_ALIAS_BLOCK = (
    "Host lima-yolobox\n"
    "  StrictHostKeyChecking no\n"
    "  UserKnownHostsFile /dev/null\n"
    "  NoHostAuthenticationForLocalhost yes\n"
    "  User user\n"
    "  Hostname 127.0.0.1\n"
    "  Port 60022\n"
)


class TestHostKeyPinning(FakeHome):
    PINNED = {
        "stricthostkeychecking": "accept-new",
        "nohostauthenticationforlocalhost": "no",
    }

    def write_lima_config(self):
        lima = os.path.join(self.home, ".lima", "yolobox")
        os.makedirs(lima, exist_ok=True)
        with open(os.path.join(lima, "ssh.config"), "w") as handle:
            handle.write(LIMA_ALIAS_BLOCK)
        with mock.patch.object(yo.ui, "err"):
            yo.ensure_agent_ssh_config()

    def effective(self, argv):
        proc = subprocess.run(
            list(argv) + ["-G", yo.box.vm_host],
            capture_output=True,
            text=True,
            check=True,
        )
        wanted = ("stricthostkeychecking", "userknownhostsfile", "nohostauth")
        lines = [line for line in proc.stdout.splitlines() if line.startswith(wanted)]
        return dict(line.split(" ", 1) for line in lines)

    def assert_pinned(self, effective):
        for keyword, value in self.PINNED.items():
            self.assertEqual(effective[keyword], value)
        self.assertEqual(effective["userknownhostsfile"], yo.known_hosts_path())

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_every_yo_role_pins_the_key_over_limas_loopback_exemption(self):
        self.write_lima_config()
        for role in (yo.OPERATOR, yo.AGENT):
            with self.subTest(role=role):
                self.assert_pinned(self.effective(["ssh"] + yo.ssh_options(role)))

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_a_bare_ssh_through_the_written_file_pins_the_key(self):
        self.write_lima_config()
        for user in ([], ["-o", "User=agent"]):
            with self.subTest(user=user):
                argv = ["ssh", "-F", yo.ssh_cfg()] + user
                self.assert_pinned(self.effective(argv))

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_yo_code_and_zed_reach_the_pin_through_the_users_include(self):
        self.write_lima_config()
        user_config = os.path.join(self.home, ".ssh", "config")
        os.makedirs(os.path.dirname(user_config))
        with open(user_config, "w") as handle:
            handle.write("Include %s\n" % yo.ssh_cfg())
        self.assert_pinned(self.effective(["ssh", "-F", user_config]))

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_first_contact_overrides_all_three_pinned_keywords(self):
        self.write_lima_config()
        effective = self.effective(["ssh"] + yo.ssh_options(yo.OPERATOR_FIRST_CONTACT))
        self.assertEqual(effective["stricthostkeychecking"], "false")
        self.assertEqual(effective["userknownhostsfile"], "/dev/null")
        self.assertEqual(effective["nohostauthenticationforlocalhost"], "yes")


class TestEnsureGuestKnownHosts(FakeHome):
    def config_path(self):
        ssh_dir = os.path.join(self.home, ".lima", "yolobox")
        os.makedirs(ssh_dir, exist_ok=True)
        return os.path.join(ssh_dir, "ssh.config")

    def write_config(self, text):
        path = self.config_path()
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_missing_file_is_not_an_error(self):
        with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()

    def test_an_unreadable_file_is_an_error_not_a_first_start(self):
        os.makedirs(self.config_path())
        with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
            with self.assertRaisesRegex(yo.YoError, "cannot read .*/ssh.config"):
                yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()

    def test_unparseable_lima_block_is_skipped_without_ssh(self):
        self.write_config(LIMA_BLOCK_MISSING_ALIAS_FIELDS)
        with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()

    def test_fetches_via_operator_and_writes_the_known_hosts_file(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()

        role, argv = fake_ssh_run.call_args[0]
        self.assertIs(role, yo.OPERATOR_FIRST_CONTACT)
        self.assertEqual(argv[0], "sh")
        self.assertIn(yo.HOST_KEY_GLOB, argv[-1])

        with open(yo.known_hosts_path(), "r") as handle:
            content = handle.read()
        self.assertEqual(content, "[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc==\n")
        self.assertEqual(stat.S_IMODE(os.stat(yo.known_hosts_path()).st_mode), 0o600)

    def test_a_comment_that_is_not_utf8_does_not_stop_the_pinning(self):
        # ensure_agent_ssh_config reads the same file with surrogateescape.
        path = self.config_path()
        with open(path, "wb") as handle:
            handle.write(b"# caf\xe9\n" + LIMA_BLOCK.encode())
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
            yo.ensure_guest_known_hosts()
        with open(yo.known_hosts_path()) as handle:
            self.assertEqual(
                handle.read(), "[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc==\n"
            )

    def test_failed_fetch_raises_naming_the_cause(self):
        self.write_config(LIMA_BLOCK)
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
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertFalse(caught.exception.message.startswith("yo:"))


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
                smallest = self.parse(command, "--memory", "8", "--disk", "40")
                self.assertEqual((smallest.memory, smallest.disk), (8, 40))
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
        memory = self.parse_error("up", "--memory", "7")
        self.assertIn("--memory must be at least 8 GiB, got 7", memory)
        self.assertIn("OOM-killed", memory)
        disk = self.parse_error("bootstrap", "--disk", "39")
        self.assertIn("--disk must be at least 40 GiB, got 39", disk)
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
        os.makedirs(yo.state_dir(), exist_ok=True)
        stub = BrokerStub(sock, status, body)
        try:
            return yo.aws_broker_health()
        finally:
            stub.close()

    def health_of(self, status, body):
        return self.health_on(yo.aws.aws_broker_ctl_sock(), status, body)

    def test_a_healthy_broker_carries_what_it_reported(self):
        info = {"pid": 5, "watch_pid": 7, "allow": ["work"]}
        health = self.health_of(200, json.dumps(info).encode())
        self.assertEqual(
            health,
            yo.BrokerHealth(yo.BrokerState.HEALTHY, info, yo.aws.aws_broker_ctl_sock()),
        )

    def test_anything_else_the_socket_says_is_unreachable(self):
        for status, body in (
            (500, b"oops"),
            (503, b"broker busy; retry\n"),
            (404, b"{}"),
            (200, b"not json"),
            (200, b"[1, 2]"),
            (200, b""),
        ):
            with self.subTest(status=status, body=body):
                self.assertIs(self.health_of(status, body), yo.BROKER_UNREACHABLE)

    def test_no_socket_is_unreachable(self):
        self.assertIs(yo.aws_broker_health(), yo.BROKER_UNREACHABLE)

    def test_a_broker_answering_only_on_the_forwarded_socket_has_no_control_socket(
        self,
    ):
        # What an older yo's broker looks like: it has no control socket.
        info = {"pid": 5, "watch_pid": 7, "allow": ["work"]}
        health = self.health_on(yo.aws_broker_sock(), 200, json.dumps(info).encode())
        self.assertEqual(
            health,
            yo.BrokerHealth(yo.BrokerState.NO_CONTROL, info, yo.aws_broker_sock()),
        )

    def test_a_broker_without_a_usable_pid_is_named_by_the_socket_that_answered(self):
        for sock in (yo.aws.aws_broker_ctl_sock(), yo.aws_broker_sock()):
            with self.subTest(sock=sock):
                health = self.health_on(sock, 200, json.dumps({"pid": None}).encode())
                with self.assertRaises(yo.YoError) as caught:
                    yo.kill_broker_in(health)
                self.assertIn(
                    "the aws-broker on %s answered /health" % sock,
                    caught.exception.message,
                )

    def test_the_control_socket_is_asked_first(self):
        # A control socket that answers decides, whatever the forwarded one says.
        os.makedirs(yo.state_dir(), exist_ok=True)
        control = BrokerStub(
            yo.aws.aws_broker_ctl_sock(), 200, json.dumps({"pid": 1}).encode()
        )
        guest = BrokerStub(yo.aws_broker_sock(), 503, b"broker busy; retry\n")
        try:
            health = yo.aws_broker_health()
        finally:
            guest.close()
            control.close()
        self.assertEqual(health.sock, yo.aws.aws_broker_ctl_sock())
        self.assertEqual(health.info, {"pid": 1})


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


def healthy(info):
    return yo.BrokerHealth(yo.BrokerState.HEALTHY, info, "/state/aws-broker.ctl.sock")


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
                health = healthy({"pid": 5, "watch_pid": value, "allow": ["work"]})
                self.assertTrue(yo.broker_needs_restart(health, 123, ["work"]))

    def test_a_broker_without_a_control_socket_needs_restart(self):
        health = yo.BrokerHealth(
            yo.BrokerState.NO_CONTROL,
            {"pid": 1, "watch_pid": 123, "allow": ["work"]},
            "/state/aws-broker.sock",
        )
        self.assertTrue(yo.broker_needs_restart(health, 123, ["work"]))

    def test_an_unreachable_broker_needs_restart(self):
        self.assertTrue(yo.broker_needs_restart(yo.BROKER_UNREACHABLE, 123, ["work"]))

    def test_matching_watch_pid_and_allowlist_does_not_restart(self):
        health = healthy({"pid": 1, "watch_pid": 123, "allow": ["work"]})
        self.assertFalse(yo.broker_needs_restart(health, 123, ["work"]))

    def test_stale_watch_pid_needs_restart(self):
        health = healthy({"pid": 1, "watch_pid": 999, "allow": ["work"]})
        self.assertTrue(yo.broker_needs_restart(health, 123, ["work"]))

    def test_allowlist_drift_needs_restart(self):
        health = healthy({"pid": 1, "watch_pid": 123, "allow": ["work"]})
        self.assertTrue(yo.broker_needs_restart(health, 123, ["personal", "work"]))


class TestEnsureAwsBrokerStaleBroker(FakeHome):
    def test_kills_the_brokers_own_pid_not_the_watched_pid(self):
        health = healthy({"pid": 111, "watch_pid": 999, "allow": ["work"]})
        killed = []
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(yo.box.driver, "keepalive_pid", return_value=555),
            mock.patch.object(yo.aws, "aws_broker_health", return_value=health),
            mock.patch.object(yo.aws, "wait_for_pid_exit"),
            mock.patch.object(yo.aws, "start_aws_broker") as fake_start,
            mock.patch.object(yo.aws, "push_aws_guest_files"),
            mock.patch.object(
                yo.os, "kill", side_effect=lambda pid, sig: killed.append(pid)
            ),
        ):
            result = yo.ensure_aws_broker()
        self.assertTrue(result)
        self.assertEqual(killed, [111])
        fake_start.assert_called_once_with(555, ["work"])


class TestEnsureAwsBrokerSerializesYos(FakeHome):
    def setUp(self):
        super().setUp()
        self.held_during = {}

    def lock_is_held(self):
        os.makedirs(yo.state_dir(), exist_ok=True)
        fd = os.open(
            os.path.join(yo.state_dir(), "aws-broker.yo.lock"), os.O_RDWR | os.O_CREAT
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # drops the lock when this probe got it
        return False

    def seen(self, step, result=None):
        """A stand-in for `step` that records whether yo's broker lock is held when it runs."""

        def record(*args, **kwargs):
            self.held_during[step] = self.lock_is_held()
            return result

        return record

    def test_checking_killing_and_starting_happen_under_one_lock(self):
        stale = healthy({"pid": 111, "watch_pid": 999, "allow": ["work"]})
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(yo.box.driver, "keepalive_pid", return_value=555),
            mock.patch.object(
                yo.aws, "aws_broker_health", side_effect=self.seen("check", stale)
            ),
            mock.patch.object(
                yo.aws, "kill_stale_broker", side_effect=self.seen("kill")
            ),
            mock.patch.object(
                yo.aws, "start_aws_broker", side_effect=self.seen("start")
            ),
            mock.patch.object(yo.aws, "push_aws_guest_files"),
        ):
            yo.ensure_aws_broker()
        self.assertEqual(self.held_during, {"check": True, "kill": True, "start": True})
        self.assertFalse(self.lock_is_held())

    def test_disabling_checks_and_kills_under_the_same_lock(self):
        stale = healthy({"pid": 111, "watch_pid": 999, "allow": []})
        with (
            mock.patch.object(
                yo.aws, "aws_broker_health", side_effect=self.seen("check", stale)
            ),
            mock.patch.object(
                yo.aws, "kill_stale_broker", side_effect=self.seen("kill")
            ),
            mock.patch.object(yo.sshx, "ssh_run"),
        ):
            yo.disable_aws_broker()
        self.assertEqual(self.held_during, {"check": True, "kill": True})
        self.assertFalse(self.lock_is_held())


class TestEnsureAwsBrokerUpgradesAnOlderBroker(FakeHome):
    def test_a_broker_without_a_control_socket_is_killed_by_its_pid_and_replaced(self):
        older = yo.BrokerHealth(
            yo.BrokerState.NO_CONTROL,
            {"pid": 111, "watch_pid": 555, "allow": ["work"]},
            "/state/aws-broker.sock",
        )
        killed = []
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(yo.box.driver, "keepalive_pid", return_value=555),
            mock.patch.object(yo.aws, "aws_broker_health", return_value=older),
            mock.patch.object(yo.aws, "wait_for_pid_exit"),
            mock.patch.object(yo.aws, "start_aws_broker") as fake_start,
            mock.patch.object(yo.aws, "push_aws_guest_files"),
            mock.patch.object(
                yo.os, "kill", side_effect=lambda pid, sig: killed.append(pid)
            ),
        ):
            yo.ensure_aws_broker()
        self.assertEqual(killed, [111])
        fake_start.assert_called_once_with(555, ["work"])


@unittest.skipUnless(
    hasattr(os, "pidfd_open") or hasattr(select, "kqueue"),
    "needs os.pidfd_open or select.kqueue to watch a pid",
)
class TestPidExitsWithin(unittest.TestCase):
    def setUp(self):
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.stdin.close)

    def test_a_running_process_has_not_exited(self):
        self.assertFalse(yo.pid_exits_within(self.child.pid, 0))

    def test_a_process_that_exits_is_seen(self):
        self.child.stdin.close()
        self.assertTrue(yo.pid_exits_within(self.child.pid, 30))

    def test_a_pid_that_is_gone_has_exited(self):
        self.child.stdin.close()
        self.child.wait()
        self.assertTrue(yo.pid_exits_within(self.child.pid, 0))

    def test_waiting_for_a_running_process_names_the_stale_broker(self):
        with self.assertRaisesRegex(yo.YoError, "did not exit within 0s"):
            yo.wait_for_pid_exit(self.child.pid, 0)


class TestStaleBrokerPidIsValidated(FakeHome):
    def test_a_pid_that_is_not_a_real_pid_is_never_signalled_and_is_an_error(self):
        # A broker that answers but cannot be stopped is reported, not skipped.
        for value in BAD_PIDS:
            with self.subTest(value=value):
                health = healthy({"pid": value, "watch_pid": 999, "allow": []})
                with mock.patch.object(yo.os, "kill") as fake_kill:
                    with self.assertRaisesRegex(yo.YoError, "cannot stop it"):
                        yo.kill_broker_in(health)
                fake_kill.assert_not_called()

    def test_an_unreachable_broker_has_nothing_to_stop(self):
        with mock.patch.object(yo.os, "kill") as fake_kill:
            yo.kill_broker_in(yo.BROKER_UNREACHABLE)
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
            argv[argv.index("--control-socket") + 1], yo.aws.aws_broker_ctl_sock()
        )
        self.assertNotEqual(yo.aws_broker_sock(), yo.aws.aws_broker_ctl_sock())

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

    def test_state_dir_and_log_are_private(self):
        self.start()
        state = yo.state_dir()
        log_dir = os.path.join(state, "aws-broker")
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(log_dir).st_mode), 0o700)
        log = os.path.join(log_dir, "broker.log")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_an_existing_permissive_dir_is_tightened(self):
        log_dir = os.path.join(yo.state_dir(), "aws-broker")
        os.makedirs(log_dir)
        os.chmod(log_dir, 0o755)
        self.start()
        self.assertEqual(stat.S_IMODE(os.stat(log_dir).st_mode), 0o700)

    def test_an_existing_log_is_truncated_not_appended_to(self):
        log_dir = os.path.join(yo.state_dir(), "aws-broker")
        os.makedirs(log_dir)
        log = os.path.join(log_dir, "broker.log")
        with open(log, "w") as handle:
            handle.write("old secrets")
        self.start()
        self.assertEqual(os.path.getsize(log), 0)


class TestStartAwsBrokerHandshakeFailures(FakeHome):
    """start_aws_broker against real children: a script standing in for aws-broker."""

    def start_with_broker_script(self, body):
        script_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, script_dir)
        script = os.path.join(script_dir, "aws-broker")
        with open(script, "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(script, 0o755)
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
                return_value=healthy({"pid": 222, "watch_pid": 1, "allow": []}),
            ),
            mock.patch.object(
                yo.aws, "kill_stale_broker", side_effect=lambda pid: killed.append(pid)
            ),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
        ):
            yo.disable_aws_broker()
        self.assertEqual(killed, [222])
        self.assertEqual(removed, [["rm", "-f", yo.GUEST_AWS_CONFIG_PATH]])


class TestDisableAwsBrokerFailsLoudly(FakeHome):
    def disable_with_ssh_exit(self, returncode, stderr):
        with (
            mock.patch.object(
                yo.aws, "aws_broker_health", return_value=yo.BROKER_UNREACHABLE
            ),
            mock.patch.object(yo.sshconfig, "ensure_agent_ssh_config"),
            mock.patch.object(
                yo.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], returncode, b"", stderr),
            ),
        ):
            yo.disable_aws_broker()

    def test_a_guest_config_that_cannot_be_removed_is_an_error_not_a_note(self):
        with self.assertRaisesRegex(
            yo.aws.BrokerDisableError, "rm -f .*aws-config.*Permission denied"
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


def initrd_probe(migrate, ask_ids=(), unlock_running=False):
    """What yo.unlock.INITRD_PROBE_COMMAND prints in an initrd in this state."""
    lines = ["migrate=%s" % migrate]
    if unlock_running:
        lines.append("unlock-fifo=/tmp/yolobox-unlock.Ab12Cd")
    for ask_id in ask_ids:
        lines += [
            "ask-begin",
            "[Ask]",
            "PID=170",
            "Socket=/run/systemd/ask-password/sck.1",
            "Id=%s" % ask_id,
            "Message=Please enter passphrase for disk root:",
            "",
            "ask-end",
        ]
    return "\n".join(lines + ["probe=end", ""])


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
        if list(argv) == ["limactl", "list", "-q"]:
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
        gone = os.path.join(self.home, "gone")
        yo.proc.delete_file(gone)
        yo.proc.delete_tree(gone)

    def test_a_path_of_the_wrong_kind_is_a_yo_error_naming_it(self):
        directory = os.path.join(self.home, "dir")
        os.mkdir(directory)
        with self.assertRaisesRegex(yo.YoError, "cannot delete .*dir: "):
            yo.proc.delete_file(directory)
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
        code, stdout = 0, b""
        if argv[:3] == ["limactl", "list", "-q"]:
            code = self.list_code
            stdout = "".join(name + "\n" for name in self.listed).encode()
        elif argv[:2] == ["limactl", "delete"]:
            code = self.delete_code
        elif argv[0] == "ssh" and "check" in argv:
            code = 0 if self.agent_master else 255
        elif argv[0] == "ssh" and "exit" in argv:
            self.agent_master = self.agent_master_survives_exit
        return subprocess.CompletedProcess(argv, code, stdout, b"")

    def destroy(self, yes):
        yo.cmd_destroy(argparse.Namespace(yes=yes))

    def deletes(self):
        return [call for call in self.calls if call[:2] == ["limactl", "delete"]]

    def test_yes_force_deletes_the_listed_instance(self):
        self.destroy(yes=True)
        self.assertEqual(self.deletes(), [["limactl", "delete", "--force", "yolobox"]])

    def test_a_failing_delete_is_a_yo_error(self):
        self.delete_code = 1
        with self.assertRaisesRegex(
            yo.YoError, "limactl delete --force yolobox failed"
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
    ):
        config = {"portForwards": forwards}
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
                yo.YoError, "limactl list yolobox --json failed .exit 1.: vz exploded"
            ):
                yo.LIMA.config_gaps()

    def test_a_listing_that_fails_is_not_a_missing_instance(self):
        with fake_limactl("", listing_returncode=1):
            with self.assertRaisesRegex(
                yo.YoError, "limactl list -q failed .exit 1.: limactl: list exploded"
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

    def test_present_reverse_rules_are_no_gaps(self):
        with self.mock_lima_list(self.restated_forwards()):
            self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_missing_reverse_rule_is_reported_not_raised(self):
        forwards = [
            forward
            for forward in self.restated_forwards()
            if "guestSocket" not in forward
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.LIMA.config_gaps()
        self.assertEqual(gaps, [yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK])

    def test_only_one_missing_rule_is_reported(self):
        forwards = [
            forward
            for forward in self.restated_forwards()
            if forward.get("guestSocket") != yo.AWS_BROKER_GUEST_SOCK
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.LIMA.config_gaps()
        self.assertEqual(gaps, [yo.AWS_BROKER_GUEST_SOCK])

    def test_mismatched_host_socket_is_reported(self):
        forwards = [
            {**forward, "hostSocket": "/somewhere/else.sock"}
            if forward.get("guestSocket") == yo.OP_GUEST_SOCK
            else forward
            for forward in self.restated_forwards()
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.LIMA.config_gaps()
        self.assertIn(yo.OP_GUEST_SOCK, gaps)

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

    def test_an_operator_uid_other_than_the_boxs_is_reported_unless_it_is_501(self):
        # Lima took the Mac account's uid when it created the instance.
        for value in (502, 0):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), user_uid=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [yo.GAP_OPERATOR_UID])
        with self.mock_lima_list(self.restated_forwards(), user_uid=501):
            self.assertEqual(yo.LIMA.config_gaps(), [])

    def test_a_uid_limactl_does_not_report_as_an_integer_is_a_note_not_a_gap(self):
        for value in (None, MISSING, "501", 501.5, True, [501]):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), user_uid=value):
                    self.assertEqual(yo.LIMA.config_gaps(), [])
                    (note,) = yo.LIMA.config_notes()
                self.assertIn("cannot tell whether the operator's uid is 501", note)

    def test_a_readable_uid_or_a_missing_instance_has_no_note(self):
        for value in (501, 502, 0):
            with self.subTest(value=value):
                with self.mock_lima_list(self.restated_forwards(), user_uid=value):
                    self.assertEqual(yo.LIMA.config_notes(), [])
        with fake_limactl("", 1, "whatever limactl says", listed=["other"]):
            self.assertEqual(yo.LIMA.config_notes(), [])

    def test_an_unreadable_uid_says_how_to_check_and_never_offers_to_destroy(self):
        with self.mock_lima_list(self.restated_forwards(), user_uid=MISSING):
            (message,) = yo.LIMA.config_notes()
        self.assertIn("limactl list --json yolobox", message)
        self.assertIn("yo ssh id -u", message)
        for destructive in ("destroy", "limactl delete", "bootstrap", "--set"):
            self.assertNotIn(destructive, message)

    def test_the_uid_gap_says_to_recreate_the_instance_not_to_edit_it(self):
        message = yo.LIMA.config_gap_message([yo.GAP_OPERATOR_UID])
        self.assertIn("cannot change file ownership", message)
        self.assertIn("yo destroy --yes && yo bootstrap", message)
        self.assertNotIn("--set", message)
        self.assertNotIn("portForwards", message)

    def test_the_proxy_env_remedy_sets_it_false(self):
        message = yo.LIMA.config_gap_message([yo.GAP_PROPAGATE_PROXY_ENV])
        self.assertIn("--set '.propagateProxyEnv = false'", message)
        self.assertNotIn("portForwards", message)

    def test_the_pubkey_remedy_sets_it_false(self):
        message = yo.LIMA.config_gap_message([yo.GAP_LOAD_DOT_SSH_PUB_KEYS])
        self.assertIn("--set '.ssh.loadDotSSHPubKeys = false'", message)
        self.assertNotIn("portForwards", message)

    def test_the_over_vsock_remedy_sets_it_false(self):
        message = yo.LIMA.config_gap_message([yo.GAP_SSH_OVER_VSOCK])
        self.assertIn("--set '.ssh.overVsock = false'", message)
        self.assertNotIn("portForwards", message)

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

        def reverse_socket(guest_socket, host_socket):
            return {
                "proto": "any",
                "guestIP": "127.0.0.1",
                "guestIPMustBeZero": False,
                "guestPortRange": [1, 65535],
                "guestSocket": guest_socket,
                "hostIP": "127.0.0.1",
                "hostPortRange": [0, 0],
                "hostSocket": host_socket,
                "reverse": True,
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
            reverse_socket(
                "/run/yolobox-op/agent.sock",
                self.home
                + "/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock",
            ),
            reverse_socket(
                "/run/yolobox-op/aws-broker.sock",
                self.home + "/.local/state/yolobox/aws-broker.sock",
            ),
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
                if "guestSocket" in forward or forward.get("proto") == "udp"
            ),
        ]
        self.assertEqual(self.gaps_for(old), [yo.GAP_TCP_FORWARDS])

    def test_no_usable_port_forwards_reports_every_gap_without_raising(self):
        for forwards in ([], None, ["lizard"], [None, 0, "lizard"]):
            with self.subTest(forwards=forwards):
                self.assertEqual(
                    self.gaps_for(forwards),
                    [
                        yo.OP_GUEST_SOCK,
                        yo.AWS_BROKER_GUEST_SOCK,
                        yo.GAP_UDP_FORWARDS,
                        yo.GAP_TCP_FORWARDS,
                    ],
                )

    def test_missing_instance_ha_pid_is_none(self):
        with fake_limactl("", listed=[]):
            self.assertIsNone(yo.box.driver.keepalive_pid())


class TestNoOperatorRoleForwardsTheMacsAgent(FakeHome):
    """A lima instance created with ssh.forwardAgent true writes `ForwardAgent
    yes` into its own Host block. The agent block covers the agent role and the
    herdr alias; the pin block covers every other connection through the file,
    the operator roles included, since ssh keeps the first value. Only `yo ssh`
    forwards, by its own command line."""

    def write_lima_config_forwarding(self):
        os.makedirs(yo.instance_dir(), exist_ok=True)
        with open(yo.ssh_cfg(), "w") as handle:
            handle.write(
                LIMA_ALIAS_BLOCK.replace(
                    "  User user\n", "  ForwardAgent yes\n  User user\n"
                )
            )
        with mock.patch.object(yo.ui, "err"):
            yo.ensure_agent_ssh_config()

    def effective_forward_agent(self, argv):
        completed = subprocess.run(
            list(argv) + ["-G", yo.box.vm_host],
            capture_output=True,
            text=True,
            check=True,
        )
        (line,) = [
            line
            for line in completed.stdout.splitlines()
            if line.startswith("forwardagent ")
        ]
        return line.split(" ", 1)[1]

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_every_role_reads_no_over_limas_yes(self):
        self.write_lima_config_forwarding()
        for role in (yo.OPERATOR, yo.AGENT):
            with self.subTest(role=role):
                argv = ["ssh"] + yo.ssh_options(role)
                self.assertEqual(self.effective_forward_agent(argv), "no")

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_yo_ssh_still_forwards_the_1password_socket_from_its_command_line(self):
        self.write_lima_config_forwarding()
        argv = ["ssh"] + yo.ssh_options(yo.OPERATOR) + yo.forward_agent_opts()
        self.assertEqual(self.effective_forward_agent(argv), yo.LIMA.op_sock())


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
            if "guestSocket" in forward:
                continue
            with self.subTest(forward=forward):
                once = yo.normalize_port_forward(forward)
                self.assertEqual(yo.normalize_port_forward(once), once)

    def test_an_empty_rule_is_every_port_on_loopback(self):
        rule = yo.normalize_port_forward({})
        self.assertEqual(rule["guestPortRange"], [1, 65535])
        self.assertEqual(rule["guestIP"], "127.0.0.1")


class TestLimaConfigGapMessage(unittest.TestCase):
    def test_names_the_full_array_migration_command(self):
        message = yo.LIMA.config_gap_message(
            [yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK]
        )
        self.assertIn(".portForwards = ", message)
        self.assertIn(json.dumps(yo.LIMA_PORT_FORWARDS), message)
        self.assertIn(yo.OP_GUEST_SOCK, message)
        self.assertIn(yo.AWS_BROKER_GUEST_SOCK, message)


class TestLimaSettingGapMessage(unittest.TestCase):
    def test_mounts_are_remedied_by_editing_mounts_alone(self):
        message = yo.LIMA.config_gap_message([yo.GAP_MOUNTS])
        self.assertIn("--set '.mounts = []'", message)
        self.assertNotIn("portForwards", message)

    def test_forward_agent_is_remedied_by_editing_forward_agent_alone(self):
        message = yo.LIMA.config_gap_message([yo.GAP_FORWARD_AGENT])
        self.assertIn("--set '.ssh.forwardAgent = false'", message)
        self.assertNotIn("portForwards", message)

    def test_the_udp_gap_is_remedied_by_restating_the_full_array(self):
        message = yo.LIMA.config_gap_message([yo.GAP_UDP_FORWARDS])
        self.assertIn(yo.RESTATE_PORT_FORWARDS, message)
        self.assertIn("41641", message)

    def test_the_tcp_gap_is_remedied_by_restating_the_full_array(self):
        message = yo.LIMA.config_gap_message([yo.GAP_TCP_FORWARDS])
        self.assertEqual(message.count(".portForwards = "), 1)
        self.assertIn("1024–49151", message)

    def test_a_socket_gap_the_udp_gap_and_the_tcp_gap_print_one_restate(self):
        message = yo.LIMA.config_gap_message(
            [yo.OP_GUEST_SOCK, yo.GAP_UDP_FORWARDS, yo.GAP_TCP_FORWARDS]
        )
        self.assertEqual(message.count(".portForwards = "), 1)
        self.assertIn("41641", message)
        self.assertIn("1024–49151", message)

    def test_the_udp_gap_and_the_tcp_gap_print_one_restate(self):
        message = yo.LIMA.config_gap_message([yo.GAP_UDP_FORWARDS, yo.GAP_TCP_FORWARDS])
        self.assertEqual(message.count(".portForwards = "), 1)

    def test_every_gap_kind_gets_its_own_remedy(self):
        message = yo.LIMA.config_gap_message(
            [yo.OP_GUEST_SOCK, yo.GAP_MOUNTS, yo.GAP_FORWARD_AGENT]
        )
        for needle in (
            yo.OP_GUEST_SOCK,
            ".portForwards = ",
            ".mounts = []",
            ".ssh.forwardAgent = false",
        ):
            self.assertIn(needle, message)


class TestVmUpRefusesOnlyTheLimaGapsThatBreakThePromises(FakeHome):
    def refused_vm_up(self, gaps):
        with (
            mock.patch.object(yo.LIMA, "config_gaps", return_value=gaps),
            mock.patch.object(yo.LIMA, "vm_status") as status,
            mock.patch.object(yo.agent, "luks_passphrase") as passphrase,
            mock.patch.object(yo.forwards, "vm_services") as services,
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_up()
        status.assert_not_called()
        passphrase.assert_not_called()
        services.assert_not_called()
        return caught.exception.message

    def test_ssh_over_vsock_refuses_before_the_vm_is_touched_naming_the_fix(self):
        for gaps in (
            [yo.GAP_SSH_OVER_VSOCK],
            [yo.OP_GUEST_SOCK, yo.GAP_FORWARD_AGENT, yo.GAP_SSH_OVER_VSOCK],
        ):
            with self.subTest(gaps=gaps):
                message = self.refused_vm_up(gaps)
                self.assertIn("--set '.ssh.overVsock = false'", message)

    def test_a_host_mount_refuses_before_the_vm_is_touched_naming_the_fix(self):
        # The one promise the box makes is no host mounts: a box with one must
        # not start, and the remedy edits the instance without deleting anything.
        message = self.refused_vm_up([yo.OP_GUEST_SOCK, yo.GAP_MOUNTS])
        self.assertIn("--set '.mounts = []'", message)
        self.assertNotIn("destroy", message)

    def test_every_other_lima_gap_is_only_a_note(self):
        others = [gap for gap in yo.LIMA_SETTING_GAPS if gap not in yo.LIMA_FATAL_GAPS]
        others += list(yo.LIMA_MANUAL_GAPS)
        self.assertEqual(yo.LIMA.fatal_gaps(others + [yo.OP_GUEST_SOCK, ""]), [])
        self.assertEqual(
            yo.LIMA.fatal_gaps(others + [yo.GAP_MOUNTS, yo.GAP_SSH_OVER_VSOCK]),
            [yo.GAP_MOUNTS, yo.GAP_SSH_OVER_VSOCK],
        )


class TestVmUpNoticesLimaConfigGapsInsteadOfRefusing(FakeHome):
    def vm_up_on_a_running_box(self, gaps):
        running = yo.LimaInfo(
            host_agent_pid=1,
            port_forwards=[],
            mounts=[],
            forward_agent=False,
            status="Running",
            over_vsock=False,
            propagate_proxy_env=False,
            load_dot_ssh_pub_keys=False,
            user_uid=yo.OPERATOR_GUEST_UID,
            ssh_local_port=yo.SSH_LOCAL_PORT,
        )
        with (
            mock.patch.object(yo.LIMA, "config_gaps", return_value=gaps),
            mock.patch.object(yo.lima, "lima_instance_info", return_value=running),
            mock.patch.object(yo.unlock, "stage2_up", return_value=True),
            mock.patch.object(yo.sshconfig, "ensure_agent_ssh_config"),
            mock.patch.object(yo.guest, "ensure_guest_known_hosts"),
            mock.patch.object(yo.forwards, "vm_services") as fake_services,
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            yo.vm_up()
        return fake_err, fake_services

    def test_a_gap_is_reported_on_stderr_and_the_vm_still_comes_up(self):
        fake_err, fake_services = self.vm_up_on_a_running_box([yo.OP_GUEST_SOCK])
        message = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn(yo.OP_GUEST_SOCK, message)
        fake_services.assert_called_once_with()

    def test_no_gaps_prints_nothing_about_lima_config(self):
        fake_err, _ = self.vm_up_on_a_running_box([])
        fake_err.assert_not_called()


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


class TestYoNamespace(unittest.TestCase):
    def module(self, **attributes):
        module = types.ModuleType("fake")
        vars(module).update(attributes)
        return module

    def test_a_module_is_found_by_its_name_and_a_function_by_its_owner(self):
        self.assertIs(yo.proc, sys.modules["yo.proc"])
        self.assertIs(yo.run, yo.proc.run)

    def test_a_name_no_module_defines_is_an_attribute_error(self):
        with self.assertRaisesRegex(AttributeError, "no module .* 'lizard'"):
            yo.lizard

    def test_a_name_two_modules_define_differently_is_refused(self):
        namespace = YoNamespace({"one": self.module(x=1), "two": self.module(x=2)})
        with self.assertRaisesRegex(AttributeError, "'x' is defined by one, two"):
            namespace.x

    def test_a_name_two_modules_share_as_one_object_is_found(self):
        shared = object()
        namespace = YoNamespace(
            {"one": self.module(x=shared), "two": self.module(x=shared)}
        )
        self.assertIs(namespace.x, shared)

    def test_a_write_names_the_module_to_patch(self):
        with self.assertRaisesRegex(AttributeError, r"patch yo\.<module>\.run"):
            yo.run = None

    def test_a_read_sees_a_rebinding_made_in_the_module(self):
        name = "yo_live"
        self.addCleanup(unload_yo, name)
        copy = load_yo(name)
        copy.state.set_box(copy.state.box.driver, "second")
        self.assertEqual(copy.box.name, "second")

    def test_unloading_forgets_the_package_and_its_modules_only(self):
        for name in ("yo_gone", "yo_gone_not"):
            self.addCleanup(unload_yo, name)
            load_yo(name)
        unload_yo("yo_gone")
        self.assertNotIn("yo_gone", sys.modules)
        self.assertNotIn("yo_gone.proc", sys.modules)
        self.assertIn("yo_gone_not", sys.modules)
        self.assertIn("yo_gone_not.proc", sys.modules)


class TestYoVersionLiteral(unittest.TestCase):
    # nix/pkgs/yolobox.nix (--replace-fail) and homebrew/yolobox.rb (inreplace)
    # both patch this exact line; if it is reformatted the brew build ships a
    # binary that still reports "dev" without failing.
    def test_config_has_exactly_one_line_that_is_the_dev_version_literal(self):
        lines = (yo.PKG_DIR / "config.py").read_text().splitlines()
        self.assertEqual(lines.count('YO_VERSION = "dev"'), 1)


class TestTailscaleStatusLine(unittest.TestCase):
    def probe(self, backend):
        return json.dumps({"BackendState": backend})

    def test_running_is_healthy(self):
        line = yo.tailscale_status_line(self.probe("Running"), "")
        self.assertEqual(line, yo.StatusLine("tailscale: Running"))

    def test_not_logged_in_is_optional_and_names_login(self):
        line = yo.tailscale_status_line(self.probe("NeedsLogin"), "")
        self.assertEqual(
            line,
            yo.StatusLine(
                "tailscale: not logged in (optional) — to join a tailnet: "
                "yo ssh sudo tailscale up"
            ),
        )

    def test_awaiting_approval_is_optional(self):
        line = yo.tailscale_status_line(self.probe("NeedsMachineAuth"), "")
        self.assertEqual(
            line,
            yo.StatusLine(
                "tailscale: awaiting approval in the tailnet admin console (optional)"
            ),
        )

    def test_a_stopped_backend_is_a_warning_naming_login(self):
        line = yo.tailscale_status_line(self.probe("Stopped"), "")
        self.assertEqual(
            line,
            yo.StatusLine(
                "tailscale: WARNING Stopped — fix with: yo ssh sudo tailscale up", True
            ),
        )

    def test_unparseable_output_surfaces_stderr(self):
        line = yo.tailscale_status_line("", "failed to connect to local tailscaled")
        self.assertIn(
            "status unreadable: failed to connect to local tailscaled", line.text
        )
        self.assertTrue(line.warning)

    def test_json_that_is_not_an_object_is_unreadable(self):
        for payload in ("null", "[]"):
            with self.subTest(payload=payload):
                line = yo.tailscale_status_line(payload + "\n", "")
                self.assertIn("status unreadable: " + payload, line.text)
                self.assertTrue(line.warning)

    def test_silent_failure_says_no_output(self):
        line = yo.tailscale_status_line("", "")
        self.assertIn("status unreadable: no output", line.text)

    def test_box_without_tailscale_says_to_bootstrap(self):
        line = yo.tailscale_status_line("NO_TAILSCALE\n", "")
        self.assertIn("yo bootstrap", line.text)
        self.assertFalse(line.warning)


class TestEgressStatusLine(unittest.TestCase):
    def probe(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def table(self, stdout="", stderr="", returncode=0):
        # What `sudo nft list table` answered; the unit branches below it
        # that never look at the table pass a probe that must not be called.
        return lambda: self.probe(stdout, stderr, returncode)

    def never_read(self):
        self.fail("the table must not be read for this unit state")

    def answer(self, load_state, active_state):
        return self.probe("LoadState=%s\nActiveState=%s\n" % (load_state, active_state))

    def test_a_loaded_active_unit_says_the_host_and_lan_are_blocked(self):
        self.assertEqual(
            yo.egress_status_line(
                self.answer("loaded", "active"), self.table(FIXTURE_EGRESS)
            ),
            yo.StatusLine("egress: host and LAN blocked"),
        )

    def test_a_loaded_unit_in_any_other_state_reads_the_table_and_names_the_state(self):
        recovery = "table inet yolobox-egress {\n\tchain output_recovery {\n"
        for state in ("failed", "inactive", "activating", "lizard"):
            with self.subTest(state=state):
                line = yo.egress_status_line(
                    self.answer("loaded", state), self.table(recovery)
                )
                self.assertTrue(line.warning)
                self.assertTrue(line.text.startswith("egress: WARNING recovery table"))
                self.assertIn("(yolobox-egress %s)" % state, line.text)
                self.assertIn("yo ssh sudo nixos-rebuild switch --rollback", line.text)
                self.assertIn("yo ssh journalctl -u yolobox-egress", line.text)

    def test_a_stopped_unit_over_the_full_table_is_not_a_recovery_table(self):
        # The unit has no ExecStop: stopping it leaves the full table loaded.
        for state in ("inactive", "failed"):
            for listing in (FIXTURE_EGRESS, FIXTURE_EGRESS_TUNNEL):
                with self.subTest(state=state):
                    line = yo.egress_status_line(
                        self.answer("loaded", state), self.table(listing)
                    )
                    self.assertTrue(line.warning)
                    self.assertTrue(
                        line.text.startswith(
                            "egress: WARNING full rules loaded, unit yolobox-egress %s"
                            % state
                        ),
                        line.text,
                    )
                    self.assertNotIn("recovery", line.text)

    def test_a_recovery_table_named_output_is_still_a_recovery_table(self):
        # Boxes built before the tiers called their recovery table plain "output".
        old_recovery = (
            (FIXTURES / "egress-recovery.nft")
            .read_text()
            .replace("output_recovery", "output")
        )
        line = yo.egress_status_line(
            self.answer("loaded", "failed"), self.table(old_recovery)
        )
        self.assertTrue(line.text.startswith("egress: WARNING recovery table"))

    def test_a_unit_the_box_does_not_know_means_the_box_predates_the_table(self):
        # systemctl prints ActiveState=inactive for an unknown unit.
        self.assertEqual(
            yo.egress_status_line(
                self.answer("not-found", "inactive"), self.never_read
            ),
            yo.StatusLine(
                "egress: box predates the egress table; run yo bootstrap", True
            ),
        )

    def test_a_silent_probe_is_not_checked_rather_than_recovery_mode(self):
        line = yo.egress_status_line(self.probe(), self.never_read)
        self.assertEqual(
            line, yo.StatusLine("egress: not checked — the probe answered '' (exit 0)")
        )

    def test_a_failed_ssh_is_not_checked_and_says_why(self):
        line = yo.egress_status_line(
            self.probe(
                stderr="ssh: connect to host lima-yolobox: refused\n", returncode=255
            ),
            self.never_read,
        )
        self.assertEqual(
            line,
            yo.StatusLine(
                "egress: not checked — the probe answered '' (exit 255): "
                "ssh: connect to host lima-yolobox: refused"
            ),
        )

    def test_an_unrecognised_load_state_is_not_checked(self):
        for load_state in ("lizard", "error"):
            with self.subTest(load_state=load_state):
                line = yo.egress_status_line(
                    self.answer(load_state, "active"), self.never_read
                )
                self.assertTrue(line.text.startswith("egress: not checked"))
                self.assertIn("LoadState=%s" % load_state, line.text)

    def test_a_masked_unit_warns_that_the_box_may_reach_the_host(self):
        for active_state in ("inactive", "active"):
            with self.subTest(active_state=active_state):
                self.assertEqual(
                    yo.egress_status_line(
                        self.answer("masked", active_state), self.never_read
                    ),
                    yo.StatusLine(
                        "egress: WARNING the egress table is masked — the box may reach "
                        "the host; unmask with: yo ssh sudo systemctl unmask yolobox-egress",
                        True,
                    ),
                )

    def test_a_loaded_unit_with_no_active_state_is_not_checked(self):
        line = yo.egress_status_line(self.probe("LoadState=loaded\n"), self.never_read)
        self.assertTrue(line.text.startswith("egress: not checked"))

    def active_line(self, table):
        return yo.egress_status_line(self.answer("loaded", "active"), table)

    def test_a_tunnel_profile_table_is_not_a_false_alarm(self):
        # lanAllow / tunnelInterfaces boxes carry extra accepts and a
        # postrouting chain; only the invariants are checked.
        self.assertEqual(
            self.active_line(self.table(FIXTURE_EGRESS_TUNNEL)),
            yo.StatusLine("egress: host and LAN blocked"),
        )

    def test_an_active_unit_over_a_wrong_table_gets_its_own_warning(self):
        wrong = {
            "empty": FIXTURE_EMPTY_TABLE,
            "no output chain": FIXTURE_EGRESS_TUNNEL.replace("chain output", "chain x"),
            "no output hook": FIXTURE_EGRESS_TUNNEL.replace(
                "hook output", "hook input"
            ),
            "no local accept": FIXTURE_EGRESS_TUNNEL.replace('"tailscale0", ', ""),
            "no v4 private reject": FIXTURE_EGRESS_TUNNEL.replace(
                "10.0.0.0/8", "11.0.0.0/8"
            ),
            "no v6 private reject": FIXTURE_EGRESS_TUNNEL.replace(
                "fc00::/7", "fd00::/8"
            ),
            "no reject": FIXTURE_EGRESS_TUNNEL.replace("reject", "accept"),
            "gibberish": "lizard\n",
        }
        for name, listing in wrong.items():
            with self.subTest(name=name):
                line = self.active_line(self.table(listing))
                self.assertTrue(line.warning)
                self.assertTrue(
                    line.text.startswith(
                        "egress: WARNING unit active but table looks wrong"
                    )
                )

    def test_a_missing_table_under_an_active_unit_is_the_wrong_table_warning(self):
        line = self.active_line(
            self.table(stderr="Error: No such file or directory\n", returncode=1)
        )
        self.assertIn("table looks wrong", line.text)

    def test_a_failed_table_probe_falls_back_to_the_unit_state(self):
        for stderr, code in (
            ("sudo: a password is required\n", 1),
            ("ssh: connect to host lima-yolobox: refused\n", 255),
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(
                    self.active_line(self.table(stderr=stderr, returncode=code)),
                    yo.StatusLine("egress: host and LAN blocked"),
                )


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
            mock.patch.object(
                yo.sshx, "account_status", lambda: (yo.AccountStatus.UNREACHABLE, "")
            ),
            mock.patch.object(yo.sshx, "ssh_run", ssh_run),
            mock.patch.object(yo.LIMA, "config_gaps", lambda: []),
            mock.patch.object(yo.LIMA, "config_notes", lambda: []),
            mock.patch.object(
                yo.unlock, "sshd_answers", lambda user: yo.SshOutcome.NOT_LISTENING
            ),
            mock.patch.object(yo.unlock, "ssh_banner", lambda: ""),
            mock.patch.object(yo.shutil, "which", lambda name: None),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))

        self.assertEqual((caught.exception.message, caught.exception.code), (None, 1))
        self.assertEqual(guest_calls, [])
        self.assertEqual(stdout.getvalue().count("unreachable"), 1)
        self.assertIn("yo up", stdout.getvalue())


# How long a test's fake server waits for the connection the code under test
# should make; a run that never connects ends the server, not the suite.
ACCEPT_TIMEOUT_SECONDS = 10


class TestBootStageNeverOffersStage2ARoot(unittest.TestCase):
    """boot_stage reads a real TCP banner and asks for root only off stage 2:
    root there is an invalid user, which sshd penalises per source address,
    and every host connection shares the gateway's (nix/base.md)."""

    STAGE2 = "SSH-2.0-OpenSSH_10.6 %s\r\n" % yo.STAGE2_SSH_BANNER_MARK

    def serve(self, banner):
        """A port whose sshd sends `banner`, or that nothing listens on (None)."""
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
        port = self.serve(banner)
        asked = []

        def answers(user):
            asked.append(user)
            return operator if user != "root" else root

        with (
            mock.patch.object(
                yo.sshconfig,
                "initrd_endpoint",
                return_value=yo.sshconfig.InitrdEndpoint("127.0.0.1", port, ("/k",)),
            ),
            mock.patch.object(yo.unlock, "sshd_answers", answers),
            mock.patch.object(yo.release, "host_user", return_value="op"),
            mock.patch.object(socket, "create_connection", REAL_CREATE_CONNECTION),
        ):
            return yo.boot_stage(), asked

    def test_a_marked_stage2_that_refuses_the_operator_is_never_asked_for_root(self):
        self.assertEqual(self.boot_stage(self.STAGE2), (yo.BootStage.NONE, ["op"]))

    def test_an_unmarked_sshd_is_asked_for_root_as_the_initrd(self):
        for banner in (
            "SSH-2.0-OpenSSH_10.6\r\n",
            "SSH-2.0-OpenSSH_10.6 %s-old\r\n" % yo.STAGE2_SSH_BANNER_MARK,
            "",
        ):
            with self.subTest(banner=banner):
                self.assertEqual(
                    self.boot_stage(banner), (yo.BootStage.INITRD, ["op", "root"])
                )

    def test_an_unmarked_sshd_that_refuses_both_is_unknown_not_absent(self):
        # A dead 1Password in front of an initrd looks exactly like this.
        self.assertEqual(
            self.boot_stage("SSH-2.0-OpenSSH_10.6\r\n", root=yo.SshOutcome.REFUSED),
            (yo.BootStage.UNKNOWN, ["op", "root"]),
        )

    def test_no_banner_and_no_login_is_nothing_answering(self):
        for banner in (None, ""):
            with self.subTest(banner=banner):
                self.assertEqual(
                    self.boot_stage(banner, root=yo.SshOutcome.NOT_LISTENING),
                    (yo.BootStage.NONE, ["op", "root"]),
                )

    def test_a_port_nothing_listens_on_is_asked_for_root_too(self):
        self.assertEqual(self.boot_stage(None), (yo.BootStage.INITRD, ["op", "root"]))

    def test_a_stage2_that_admits_the_operator_is_stage2_without_a_banner_read(self):
        self.assertEqual(
            self.boot_stage(None, operator=yo.SshOutcome.ANSWERED),
            (yo.BootStage.STAGE2, ["op"]),
        )


class TestLimaStopHangOffersTheKillOnlyPastTheInitrd(unittest.TestCase):
    def test_the_kill_is_conditional_on_ssh_answering(self):
        with (
            mock.patch.object(yo.LIMA, "vm_status", return_value="Running"),
            mock.patch.object(
                yo.proc, "run", side_effect=yo.RunTimeout("limactl stop timed out")
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.LIMA.stop()
        message = caught.exception.message
        # "answers" would admit the initrd's host-key or permission errors.
        self.assertIn(
            "If yo ssh true succeeds (exits 0 and prints nothing), the box is past "
            "its initrd",
            message,
        )
        self.assertIn("If it fails in any way", message)
        self.assertIn("do not kill the hostagent", message)
        self.assertNotIn(yo.LIMA.force_stop_command(), message)


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
                stop()
        return caught.exception

    def test_a_polite_stop_carries_the_hang_recovery_and_its_exit(self):
        refused = self.failed_stop(yo.LIMA.stop, ["Running", "Running"])
        self.assertEqual(refused.reason, "limactl stop yolobox failed (exit 1)")
        self.assertIn("If yo ssh true succeeds", refused.message)
        self.assertIn("do not kill the hostagent", refused.message)

    def test_a_forced_stop_carries_the_hostagent_kill_alone(self):
        refused = self.failed_stop(yo.LIMA.force_stop, ["Running"])
        self.assertEqual(refused.reason, "limactl stop --force yolobox failed (exit 1)")
        self.assertIn("kill -9 any 'limactl hostagent'", refused.message)
        self.assertNotIn("systemctl reboot", refused.message)


class TestLimaInitrdTimeoutNamesItsBootLog(unittest.TestCase):
    def test_serialv_log_keeps_the_boot_so_it_is_named(self):
        text = yo.unlock.initrd_timeout_error(False).message
        self.assertIn(
            "its boot log shows how far it got: ~/.lima/yolobox/serialv.log", text
        )


class TestUnknownStageOffersNoForceOnLima(unittest.TestCase):
    def test_the_way_out_is_lima_s_key_and_never_a_forced_stop(self):
        for login in (None, "lizard"):
            with self.subTest(login=login):
                text = yo.unknown_stage_message(login, yo.vm.RERUN)
                self.assertIn("~/.lima/_config/user", text)
                # The next step when the key is right: Lima's own boot log.
                self.assertIn("~/.lima/yolobox/serialv.log", text)
                self.assertIn(yo.unlock.MIGRATE_UNIT_DESCRIPTION, text)
                self.assertNotIn(yo.LIMA.force_stop_command(), text)
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
            return yo.unlock.ssh_banner()

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


class TestClassifyDisk(unittest.TestCase):
    def test_each_filesystem_type_has_its_state(self):
        for fstype, state in (
            ("", yo.DiskState.UNREADABLE),
            ("ext4", yo.DiskState.PLAIN_EXT4),
            ("crypto_LUKS", yo.DiskState.ENCRYPTED),
            ("xfs", yo.DiskState.OTHER),
            ("lizard", yo.DiskState.OTHER),
        ):
            with self.subTest(fstype=fstype):
                self.assertIs(yo.classify_disk(fstype), state)


class TestSshArgv(FakeHome):
    def test_it_is_ssh_then_the_roles_options_then_the_tail(self):
        argv = yo.sshx.ssh_argv(yo.AGENT, "-t", "host", mux=True)
        self.assertEqual(
            argv, ["ssh"] + yo.ssh_options(yo.AGENT, mux=True) + ["-t", "host"]
        )

    def test_the_multiplexing_off_by_default_says_so(self):
        self.assertIn("ControlPath=none", yo.sshx.ssh_argv(yo.OPERATOR))


class TestPortOnLima(unittest.TestCase):
    def test_yo_port_exits_with_the_usage_code_because_lima_mirrors_ports_itself(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.LIMA.forward_port(8080)
        self.assertEqual(caught.exception.code, 64)
        self.assertEqual(yo.USAGE_EXIT, 64)


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
                completed = mock.Mock(returncode=returncode, stdout="", stderr=stderr)
                with mock.patch.object(yo.sshx, "ssh_run", return_value=completed):
                    with self.assertRaisesRegex(
                        yo.YoError,
                        r"root usage from df \(exit %d\): %s" % (returncode, stderr),
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
            return_value=mock.Mock(returncode=0, stdout="1 \u00b2 1\n"),
        ):
            with self.assertRaisesRegex(yo.YoError, "reports no memory, disk and CPUs"):
                yo.LIMA.current_size()


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
