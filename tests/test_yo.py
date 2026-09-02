import importlib.machinery
import importlib.util
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

YO_PATH = str(Path(__file__).resolve().parent.parent / "yo")


def load_yo():
    loader = importlib.machinery.SourceFileLoader("yo", YO_PATH)
    spec = importlib.util.spec_from_loader("yo", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["yo"] = module
    loader.exec_module(module)
    return module


yo = load_yo()


def with_home(path):
    return mock.patch.dict(os.environ, {"HOME": path}, clear=False)


class FakeHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        patcher = with_home(self.home)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMirror(FakeHome):
    def test_home_and_subdirectory(self):
        self.assertEqual(yo.mirror(self.home), yo.AGENT_HOME)
        self.assertEqual(
            yo.mirror(self.home + "/a/b"), yo.AGENT_HOME + "/a/b"
        )

    def test_outside_home(self):
        self.assertIsNone(yo.mirror("/etc/passwd"))
        self.assertIsNone(yo.mirror(self.home + "x/y"))

    def test_symlinked_spelling_is_not_resolved(self):
        target = os.path.join(self.home, "Developer")
        os.mkdir(target)
        link = os.path.join(self.home, "wrk")
        os.symlink(target, link)
        self.assertEqual(yo.mirror(link + "/proj"), yo.AGENT_HOME + "/wrk/proj")


class TestGuestPath(unittest.TestCase):
    def test_accepts_home_and_children(self):
        self.assertEqual(yo.guest_path(yo.AGENT_HOME), yo.AGENT_HOME)
        self.assertEqual(yo.guest_path(yo.AGENT_HOME + "/x"), yo.AGENT_HOME + "/x")

    def test_rejects_sibling_prefix(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path("/home/agentx")

    def test_rejects_traversal(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path(yo.AGENT_HOME + "/../x")

    def test_rejects_newline(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path(yo.AGENT_HOME + "/a\nb")


class TestSendEnv(unittest.TestCase):
    def test_only_names_reach_argv(self):
        opts, env = yo.send_env({"HERDR_PANE_ID": "w1:p2 x"})
        self.assertEqual(opts, ["-o", "SendEnv=HERDR_PANE_ID"])
        self.assertEqual(env["HERDR_PANE_ID"], "w1:p2 x")
        self.assertNotIn("w1:p2 x", " ".join(opts))


class TestHerdForward(FakeHome):
    def setUp(self):
        super().setUp()
        self.sock_path = os.path.join(self.home, "herdr.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.sock_path)
        self.addCleanup(self.server.close)

    def herd_env(self, **overrides):
        values = {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p2",
            "HERDR_SOCKET_PATH": self.sock_path,
        }
        values.update(overrides)
        return mock.patch.dict(os.environ, values, clear=False)

    def test_builds_guest_socket_and_env(self):
        with self.herd_env():
            forward = yo.herd_forward()
        guest_sock = "/run/yolobox/herd-host.w1_p2.sock"
        self.assertEqual(forward.guest_sock, guest_sock)
        self.assertEqual(forward.env["YOLOBOX_HERD"], "1")
        self.assertEqual(forward.env["HERDR_SOCKET_PATH"], guest_sock)
        self.assertEqual(forward.env["HERDR_PANE_ID"], "w1:p2")
        self.assertIn("-R", forward.ssh_opts)
        self.assertIn("%s:%s" % (guest_sock, self.sock_path), forward.ssh_opts)

    def test_pane_id_is_sanitised_not_refused(self):
        with self.herd_env(HERDR_PANE_ID="../x"):
            forward = yo.herd_forward()
        self.assertEqual(forward.guest_sock, "/run/yolobox/herd-host.___x.sock")


class TestSshRunArgv(FakeHome):
    def capture_argv(self, **kwargs):
        seen = {}

        def fake_run(argv, **rest):
            seen["argv"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo, "run", fake_run):
            yo.ssh_run(yo.OPERATOR, ["true"], **kwargs)
        return seen["argv"]

    def test_no_stdin_closes_it_with_dash_n(self):
        self.assertIn("-n", self.capture_argv())

    def test_piped_stdin_keeps_it_open(self):
        self.assertNotIn("-n", self.capture_argv(stdin=b"script"))

    def test_agent_role_carries_user_and_control_path(self):
        argv = yo.ssh_base(yo.AGENT, mux=True)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=%s/.lima/yolobox/ssh-agent.sock" % self.home, argv)


INCLUDE = "Include ~/.lima/yolobox/ssh.config\n"


class TestSshConfigIncludeLine(FakeHome):
    def write_config(self, text):
        ssh_dir = os.path.join(self.home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "config"), "w") as handle:
            handle.write(text)

    def test_missing_file_is_none(self):
        self.assertIsNone(yo.ssh_config_include_line())

    def test_absent_include_is_none(self):
        self.write_config("Host other\n  User x\n")
        self.assertIsNone(yo.ssh_config_include_line())

    def test_present_include_is_its_line_number(self):
        self.write_config("Host other\n  User x\n" + INCLUDE)
        self.assertEqual(yo.ssh_config_include_line(), 3)


def isatty(value):
    fake_stdin = mock.Mock()
    fake_stdin.isatty.return_value = value
    return mock.patch.object(yo.sys, "stdin", fake_stdin)


class TestEnsureSshConfigInclude(FakeHome):
    def setUp(self):
        super().setUp()
        for name in ("err", "out"):
            patcher = mock.patch.object(yo, name)
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
        self.assertEqual(self.read_config(), yo.INCLUDE_LINE + "\nHost other\n  User x\n")

    def test_accepted_creates_ssh_dir_and_config_when_absent(self):
        with isatty(True):
            with mock.patch("builtins.input", return_value="yes"):
                yo.ensure_ssh_config_include(refuse=True)
        ssh_dir = os.path.join(self.home, ".ssh")
        self.assertEqual(stat.S_IMODE(os.stat(ssh_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path()).st_mode), 0o600)
        self.assertEqual(self.read_config(), yo.INCLUDE_LINE + "\n")

    def test_accepted_a_second_time_does_not_duplicate(self):
        self.write_config("Host other\n")
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                yo.ensure_ssh_config_include(refuse=True)
                yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(self.read_config().count(yo.INCLUDE_LINE), 1)

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
                yo.ensure_ssh_config_include(refuse=False)
        self.assertEqual(self.read_config(), original)

    def test_non_tty_never_prompts_and_refuses(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(False):
            with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.read_config(), original)

    def test_non_tty_without_refuse_returns(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(False):
            with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
                yo.ensure_ssh_config_include(refuse=False)
        self.assertEqual(self.read_config(), original)


LIMA_BLOCK = "Host lima-yolobox\n  ControlPath ~/.lima/yolobox/ssh.sock\n  User agent\n"


class TestEnsureAgentSshConfig(FakeHome):
    def config_path(self):
        ssh_dir = os.path.join(self.home, ".lima", "yolobox")
        os.makedirs(ssh_dir, exist_ok=True)
        return os.path.join(ssh_dir, "ssh.config")

    def read_config(self):
        with open(self.config_path(), "r") as handle:
            return handle.read()

    def test_missing_file_is_not_an_error(self):
        yo.ensure_agent_ssh_config()

    def test_yolobox_block_precedes_limas_own_control_path(self):
        path = self.config_path()
        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertLess(
            content.index("ssh-agent.sock"), content.index("ssh.sock")
        )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_second_call_is_byte_identical(self):
        path = self.config_path()
        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()
        first = self.read_config()

        yo.ensure_agent_ssh_config()

        self.assertEqual(self.read_config(), first)

    def test_lima_regenerated_file_is_repaired_without_duplicating(self):
        path = self.config_path()
        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()

        with open(path, "w") as handle:
            handle.write(LIMA_BLOCK)
        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertEqual(content.count("ssh-agent.sock"), 1)
        self.assertLess(content.index("ssh-agent.sock"), content.index("ssh.sock"))


class TestParsePairingUrl(unittest.TestCase):
    def test_accepts_loopback(self):
        url = "http://127.0.0.1:3773/?token=x"
        self.assertEqual(yo.parse_pairing_url("  Pairing URL: %s\n" % url), url)

    def test_rejects_anything_but_loopback_http(self):
        urls = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://evil.example/",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(yo.YoError):
                    yo.parse_pairing_url("Pairing URL: %s\n" % url)

    def test_none_without_a_pairing_line(self):
        self.assertIsNone(yo.parse_pairing_url("Grok CLI health check failed\n"))


class Reached(Exception):
    pass


class TestDiskGrowSize(unittest.TestCase):
    def parse(self, arg):
        def fake_run(argv, **kwargs):
            raise Reached()

        with mock.patch.object(yo, "run", fake_run):
            yo.cmd_disk_grow([arg])

    def test_whole_number_passes_validation(self):
        with self.assertRaises(Reached):
            self.parse("10")

    def test_rejects_non_whole_number(self):
        for arg in ["1e3", "-1"]:
            with self.subTest(arg=arg):
                with self.assertRaises(yo.YoError) as caught:
                    self.parse(arg)
                self.assertIn("whole number of GiB", caught.exception.message)

    def test_rejects_zero(self):
        with self.assertRaises(yo.YoError) as caught:
            self.parse("0")
        self.assertIn("greater than 0 GiB", caught.exception.message)

    def test_requires_exactly_one_argument(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.cmd_disk_grow([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
