import importlib.machinery
import importlib.util
import os
import socket
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
    # dataclasses resolves its annotations through sys.modules, so the module
    # has to be registered before its body runs.
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
    def test_home_itself(self):
        self.assertEqual(yo.mirror(self.home), yo.AGENT_HOME)

    def test_subdirectory_with_spaces(self):
        self.assertEqual(
            yo.mirror(self.home + "/a b/c"), yo.AGENT_HOME + "/a b/c"
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

    def test_rejects_outside(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path("/etc")

    def test_rejects_traversal(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path(yo.AGENT_HOME + "/../x")

    def test_rejects_newline(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path(yo.AGENT_HOME + "/a\nb")


class TestRemote(unittest.TestCase):
    def test_quotes_shell_metacharacters(self):
        self.assertEqual(yo.remote(["cd", "a b;rm -rf /"]), "cd 'a b;rm -rf /'")


class TestSendEnv(unittest.TestCase):
    def test_only_names_reach_argv(self):
        opts, env = yo.send_env({"HERDR_PANE_ID": "w1:p2 x"})
        self.assertEqual(opts, ["-o", "SendEnv=HERDR_PANE_ID"])
        self.assertEqual(env["HERDR_PANE_ID"], "w1:p2 x")
        self.assertNotIn("w1:p2 x", " ".join(opts))

    def test_refuses_name_outside_whitelist(self):
        with self.assertRaises(yo.YoError):
            yo.send_env({"PATH": "/tmp"})


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

    def test_colon_in_host_socket_is_refused(self):
        colon_path = os.path.join(self.home, "a:b.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(colon_path)
        self.addCleanup(server.close)
        with self.herd_env(HERDR_SOCKET_PATH=colon_path):
            with self.assertRaises(yo.YoError):
                yo.herd_forward()

    def test_absent_outside_a_herdr_pane(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(yo.herd_forward())

    def test_absent_when_socket_is_missing(self):
        with self.herd_env(HERDR_SOCKET_PATH=os.path.join(self.home, "gone.sock")):
            self.assertIsNone(yo.herd_forward())


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

    def test_nomux_disables_multiplexing(self):
        self.assertIn("ControlPath=none", yo.ssh_base(yo.AGENT))


class TestParseHerdVersion(unittest.TestCase):
    def test_extracts_semver(self):
        self.assertEqual(yo.parse_herd_version("herdr 0.8.2\r\n"), "0.8.2")

    def test_none_without_digits(self):
        self.assertIsNone(yo.parse_herd_version("command not found\n"))


BLOCK = "Match host lima-yolobox user agent\n  ControlPath ~/.lima/yolobox/ssh-agent.sock\n"
INCLUDE = "Include ~/.lima/yolobox/ssh.config\n"


class TestEditorSshConfig(FakeHome):
    def write_config(self, text):
        ssh_dir = os.path.join(self.home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "config"), "w") as handle:
            handle.write(text)

    def test_missing_block(self):
        self.write_config(INCLUDE)
        self.assertEqual(yo.editor_ssh_config().problem, "missing")

    def test_incomplete_block(self):
        self.write_config("Match host lima-yolobox user agent\n  User agent\n" + INCLUDE)
        self.assertEqual(yo.editor_ssh_config().problem, "incomplete")

    def test_block_below_include(self):
        self.write_config(INCLUDE + BLOCK)
        self.assertEqual(yo.editor_ssh_config().problem, "below")

    def test_correct_config(self):
        self.write_config(BLOCK + INCLUDE)
        config = yo.editor_ssh_config()
        self.assertIsNone(config.problem)
        self.assertEqual(config.match_line, 1)
        self.assertEqual(config.include_line, 3)


class TestParsePairingUrl(unittest.TestCase):
    def test_accepts_loopback(self):
        url = "http://127.0.0.1:3773/?token=x"
        self.assertEqual(yo.parse_pairing_url("  Pairing URL: %s\n" % url), url)

    def test_rejects_file_scheme(self):
        with self.assertRaises(yo.YoError):
            yo.parse_pairing_url("Pairing URL: file:///etc/passwd\n")

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(yo.YoError):
            yo.parse_pairing_url("Pairing URL: javascript:alert(1)\n")

    def test_rejects_remote_host(self):
        with self.assertRaises(yo.YoError):
            yo.parse_pairing_url("Pairing URL: http://evil.example/\n")

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

    def test_rejects_scientific_notation(self):
        with self.assertRaises(yo.YoError) as caught:
            self.parse("1e3")
        self.assertIn("whole number of GiB", caught.exception.message)

    def test_rejects_negative(self):
        with self.assertRaises(yo.YoError) as caught:
            self.parse("-1")
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
