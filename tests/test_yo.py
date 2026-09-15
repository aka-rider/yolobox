import contextlib
import importlib.machinery
import importlib.util
import io
import os
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
        opts, env = yo.send_env({"AWS_REGION": "w1:p2 x"})
        self.assertEqual(opts, ["-o", "SendEnv=AWS_REGION"])
        self.assertEqual(env["AWS_REGION"], "w1:p2 x")
        self.assertNotIn("w1:p2 x", " ".join(opts))


class TestSshRunArgv(FakeHome):
    def capture_argv(self, role=None, **kwargs):
        seen = {}

        def fake_run(argv, **rest):
            seen["argv"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo, "run", fake_run):
            yo.ssh_run(role or yo.OPERATOR, ["true"], **kwargs)
        return seen["argv"]

    def test_no_stdin_closes_it_with_dash_n(self):
        self.assertIn("-n", self.capture_argv())

    def test_piped_stdin_keeps_it_open(self):
        self.assertNotIn("-n", self.capture_argv(stdin=b"script"))

    def test_agent_role_carries_user_and_control_path(self):
        argv = yo.ssh_base(yo.AGENT, mux=True)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=%s/.lima/yolobox/ssh-agent.sock" % self.home, argv)

    def test_send_env_over_a_shared_control_master_is_refused(self):
        opts = yo.send_env({"AWS_REGION": "x"})[0]
        with self.assertRaises(yo.YoError):
            self.capture_argv(yo.AGENT, extra_opts=opts, mux=True)

    def test_send_env_on_an_unshared_connection_reaches_argv(self):
        opts = yo.send_env({"AWS_REGION": "x"})[0]
        argv = self.capture_argv(yo.AGENT, extra_opts=opts, mux=False)
        self.assertIn("ControlPath=none", argv)
        self.assertIn("SendEnv=AWS_REGION", argv)


class TestResolveCpOperands(unittest.TestCase):
    MIRROR = "/home/agent/wrk/proj"

    def test_requires_a_source_and_a_destination(self):
        with self.assertRaises(yo.YoError):
            yo.resolve_cp_operands([":a"], self.MIRROR)

    def test_all_local_is_refused_and_names_cp(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.resolve_cp_operands(["a.txt", "b.txt"], self.MIRROR)
        self.assertIn("cp", caught.exception.message)

    def test_mixed_side_sources_are_refused(self):
        with self.assertRaises(yo.YoError):
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
        with self.assertRaises(yo.YoError):
            yo.resolve_cp_operands(["a.txt", ":/etc/passwd"], self.MIRROR)

    def test_guest_traversal_outside_home_is_refused(self):
        with self.assertRaises(yo.YoError):
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
            yo.cp_mkdir_target(":", "/home/agent/wrk/proj", 1), "/home/agent/wrk/proj"
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
            yo.cp_mkdir_target(":file.txt", "/home/agent/wrk/proj/file.txt", 1),
            "/home/agent/wrk/proj",
        )


class TestCpArgv(FakeHome):
    def run_cp(self, paths):
        calls = {"run": None, "ssh_run": []}

        def fake_run(argv, **kwargs):
            calls["run"] = list(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            calls["ssh_run"].append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo, "run", fake_run), mock.patch.object(
            yo, "ssh_run", fake_ssh_run
        ):
            args = yo.build_parser().parse_args(["cp"] + paths)
            yo.cmd_cp(args)
        return calls

    def test_to_guest_argv_carries_scp_flags_and_no_forward_agent(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        self.assertIn("-r", argv)
        self.assertIn("-p", argv)
        self.assertIn("-F", argv)
        self.assertIn("User=agent", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertNotIn("ForwardAgent", " ".join(argv))

    def test_to_guest_remote_operand_is_prefixed_with_vm_host(self):
        argv = self.run_cp(["a.txt", ":~/x"])["run"]
        self.assertIn("lima-yolobox:/home/agent/x", argv)

    def test_mkdir_runs_before_scp_for_to_guest(self):
        calls = self.run_cp(["a.txt", ":~/x"])
        self.assertEqual(calls["ssh_run"], [["true"], ["mkdir", "-p", "/home/agent"]])

    def test_no_mkdir_for_from_guest(self):
        calls = self.run_cp([":~/x", "dest.txt"])
        self.assertEqual(calls["ssh_run"], [["true"]])
        self.assertIn("lima-yolobox:/home/agent/x", calls["run"])
        self.assertIn("dest.txt", calls["run"])


STALE_REMOTE = "ssh://lima-yolobox/home/xiii.guest/wrk/rune"


class TestLinkRemoteAction(unittest.TestCase):
    def setUp(self):
        self.wanted = "ssh://agent@lima-yolobox/home/agent/wrk/rune"

    def test_no_remote_is_added(self):
        self.assertEqual(yo.link_remote_action(None, self.wanted), "add")
        self.assertEqual(yo.link_remote_action("", self.wanted), "add")

    def test_matching_remote_is_kept(self):
        self.assertEqual(yo.link_remote_action(self.wanted, self.wanted), "keep")

    def test_stale_yolobox_remote_is_updated(self):
        self.assertEqual(yo.link_remote_action(STALE_REMOTE, self.wanted), "update")

    def test_foreign_remote_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.link_remote_action("git@github.com:x/y.git", self.wanted)
        self.assertIn("git@github.com:x/y.git", caught.exception.message)
        self.assertIn("git remote remove yolobox", caught.exception.message)


class TestLink(FakeHome):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.home, "wrk", "rune")
        os.makedirs(self.project)
        self.wanted = "ssh://agent@lima-yolobox%s/wrk/rune" % yo.AGENT_HOME
        patcher = mock.patch.object(yo, "logical_cwd", lambda: self.project)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("require_agent_account", "agent_out"):
            patcher = mock.patch.object(yo, name, mock.Mock(return_value=""))
            patcher.start()
            self.addCleanup(patcher.stop)

    def link(self, get_url):
        calls = []

        def fake_run(argv, **rest):
            argv = list(argv)
            calls.append(argv)
            if "get-url" in argv:
                return get_url
            return mock.Mock(returncode=0, stdout="", stderr="")

        stderr = io.StringIO()
        args = yo.build_parser().parse_args(["link"])
        with mock.patch.object(yo, "run", fake_run):
            with contextlib.redirect_stderr(stderr):
                yo.cmd_link(args)
        return calls, stderr.getvalue()

    def remote_subcommands(self, calls):
        return [argv[3:] for argv in calls if argv[3:4] == ["remote"]]

    def test_stale_remote_is_updated_in_place(self):
        calls, stderr = self.link(
            mock.Mock(returncode=0, stdout=STALE_REMOTE + "\n", stderr="")
        )
        subcommands = self.remote_subcommands(calls)
        self.assertIn(["remote", "set-url", "yolobox", self.wanted], subcommands)
        self.assertNotIn("add", [argv[1] for argv in subcommands])
        self.assertIn("pointed at " + STALE_REMOTE, stderr)
        self.assertIn("updated to " + self.wanted, stderr)

    def test_missing_remote_is_added(self):
        calls, stderr = self.link(mock.Mock(returncode=128, stdout="", stderr=""))
        subcommands = self.remote_subcommands(calls)
        self.assertIn(["remote", "add", "yolobox", self.wanted], subcommands)
        self.assertNotIn("set-url", [argv[1] for argv in subcommands])
        self.assertEqual(stderr, "")

    def test_matching_remote_is_reported_and_left_alone(self):
        calls, stderr = self.link(
            mock.Mock(returncode=0, stdout=self.wanted, stderr="")
        )
        subcommands = self.remote_subcommands(calls)
        self.assertEqual([argv[1] for argv in subcommands], ["get-url"])
        self.assertIn("already points at " + self.wanted, stderr)


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


LIMA_BLOCK = (
    "Host lima-yolobox\n"
    '  IdentityFile "/Users/xiii/.lima/_config/user"\n'
    "  StrictHostKeyChecking no\n"
    "  User xiii\n"
    "  ControlMaster auto\n"
    '  ControlPath "/Users/xiii/.lima/yolobox/ssh.sock"\n'
    "  ControlPersist yes\n"
    "  Hostname 127.0.0.1\n"
    "  Port 60022\n"
)

LIMA_BLOCK_MISSING_ALIAS_FIELDS = "Host lima-yolobox\n  ControlPath ~/.lima/yolobox/ssh.sock\n  User agent\n"


class TestEnsureAgentSshConfig(FakeHome):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(yo, "err")
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
        self.assertLess(
            content.index("ssh-agent.sock"), content.index("ssh.sock")
        )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

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
        self.assertNotIn("ForwardAgent", alias)

    def test_herdr_alias_takes_hostname_port_identityfile_from_lima(self):
        self.write_config(LIMA_BLOCK)

        yo.ensure_agent_ssh_config()

        alias = self.read_config().split("Host yolobox\n", 1)[1]
        self.assertIn("  Hostname 127.0.0.1\n", alias)
        self.assertIn("  Port 60022\n", alias)
        self.assertIn('  IdentityFile "/Users/xiii/.lima/_config/user"\n', alias)

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
        self.write_config("Host somewhere-else\n  Hostname 1.2.3.4\n  Port 22\n  IdentityFile x\n")

        yo.ensure_agent_ssh_config()

        content = self.read_config()
        self.assertNotIn("Host yolobox\n", content)
        self.err.assert_called_once()


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

    def test_unicode_digit_passes_validation(self):
        with self.assertRaises(Reached):
            self.parse("٥")  # Arabic-indic digit five

    def test_rejects_non_whole_number(self):
        self.assertIn("whole number of GiB", self.parse_error("1e3"))

    def test_rejects_zero(self):
        self.assertIn("greater than 0 GiB", self.parse_error("0"))

    def test_rejects_negative(self):
        # int("-1") parses cleanly, so this is caught by the positivity check,
        # not the whole-number check — see the deviation note in the report.
        self.assertIn("greater than 0 GiB", self.parse_error("-1"))

    def test_requires_exactly_one_argument(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["disk-grow"])
        self.assertEqual(caught.exception.code, 2)


def with_platform(name):
    return mock.patch.object(yo.sys, "platform", name)


class TestOpSock(FakeHome):
    def test_darwin_uses_the_1password_group_container(self):
        with with_platform("darwin"):
            sock = yo.op_sock()
        self.assertEqual(
            sock, os.path.join(self.home, "Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock")
        )

    def test_linux_uses_the_dotfile_socket(self):
        with with_platform("linux"):
            sock = yo.op_sock()
        self.assertEqual(sock, os.path.join(self.home, ".1password/agent.sock"))


class TestOpenUrl(unittest.TestCase):
    def test_darwin_execs_open(self):
        with with_platform("darwin"):
            with mock.patch.object(yo, "exec_process") as fake_exec:
                yo.open_url("http://127.0.0.1:3773/?t=1")
        fake_exec.assert_called_once_with(["open", "http://127.0.0.1:3773/?t=1"])

    def test_linux_execs_xdg_open(self):
        with with_platform("linux"):
            with mock.patch.object(yo, "exec_process") as fake_exec:
                yo.open_url("http://127.0.0.1:3773/?t=1")
        fake_exec.assert_called_once_with(["xdg-open", "http://127.0.0.1:3773/?t=1"])


class TestCodeCli(unittest.TestCase):
    def test_prefers_whatever_is_on_path_regardless_of_platform(self):
        with with_platform("linux"):
            with mock.patch.object(yo.shutil, "which", return_value="/usr/bin/code"):
                self.assertEqual(yo.code_cli(), "/usr/bin/code")

    def test_darwin_falls_back_to_the_app_bundle(self):
        with with_platform("darwin"):
            with mock.patch.object(yo.shutil, "which", return_value=None):
                with mock.patch.object(yo.os, "access", return_value=True):
                    cli = yo.code_cli()
        self.assertEqual(cli, "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")

    def test_darwin_refuses_when_neither_path_nor_the_app_bundle_has_it(self):
        with with_platform("darwin"):
            with mock.patch.object(yo.shutil, "which", return_value=None):
                with mock.patch.object(yo.os, "access", return_value=False):
                    with self.assertRaises(yo.YoError):
                        yo.code_cli()

    def test_linux_refuses_naming_path_as_the_remedy(self):
        with with_platform("linux"):
            with mock.patch.object(yo.shutil, "which", return_value=None):
                with self.assertRaises(yo.YoError) as caught:
                    yo.code_cli()
        self.assertIn("PATH", caught.exception.message)


class TestToolHint(unittest.TestCase):
    def test_darwin_names_the_brew_formula(self):
        with with_platform("darwin"):
            self.assertEqual(yo.tool_hint("limactl"), "brew install lima")
            self.assertEqual(yo.tool_hint("fzf"), "brew install fzf")
            self.assertEqual(yo.tool_hint("aws"), "brew install awscli")

    def test_linux_names_the_flake_for_limactl_and_fzf(self):
        with with_platform("linux"):
            self.assertEqual(yo.tool_hint("limactl"), "nix run github:aka-rider/yolobox")
            self.assertEqual(yo.tool_hint("fzf"), "nix run github:aka-rider/yolobox")

    def test_linux_points_aws_at_the_distro_package(self):
        with with_platform("linux"):
            hint = yo.tool_hint("aws")
        self.assertIn("aws", hint)
        self.assertIn("distro", hint)


class FakeRouteSocket:
    def __init__(self, ip=None, connect_error=None):
        self.ip = ip
        self.connect_error = connect_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def connect(self, address):
        if self.connect_error is not None:
            raise self.connect_error

    def getsockname(self):
        return (self.ip, 51413)


class TestPairBaseUrl(unittest.TestCase):
    def test_darwin_uses_scutil_and_the_local_domain(self):
        with with_platform("darwin"):
            with mock.patch.object(yo, "run", return_value=mock.Mock(stdout="mymac\n")):
                url = yo.pair_base_url()
        self.assertEqual(url, "http://mymac.local:3773")

    def test_linux_uses_the_primary_route_ip(self):
        with with_platform("linux"):
            with mock.patch.object(
                yo.socket, "socket", return_value=FakeRouteSocket(ip="192.168.1.42")
            ):
                with mock.patch.object(yo, "err") as fake_err:
                    url = yo.pair_base_url()
        self.assertEqual(url, "http://192.168.1.42:3773")
        fake_err.assert_called_once()

    def test_linux_refuses_naming_base_url_as_the_override_when_there_is_no_route(self):
        with with_platform("linux"):
            with mock.patch.object(
                yo.socket, "socket", return_value=FakeRouteSocket(connect_error=OSError("no route"))
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.pair_base_url()
        self.assertIn("--base-url", caught.exception.message)


class TestRequireKvm(unittest.TestCase):
    def test_darwin_is_a_no_op(self):
        with with_platform("darwin"):
            with mock.patch.object(yo.os.path, "exists", return_value=False) as fake_exists:
                yo.require_kvm()
        fake_exists.assert_not_called()

    def test_linux_refuses_when_the_device_is_absent(self):
        with with_platform("linux"):
            with mock.patch.object(yo.os.path, "exists", return_value=False):
                with self.assertRaises(yo.YoError) as caught:
                    yo.require_kvm()
        self.assertIn("/dev/kvm", caught.exception.message)

    def test_linux_refuses_when_the_device_is_not_readable_or_writable(self):
        with with_platform("linux"):
            with mock.patch.object(yo.os.path, "exists", return_value=True):
                with mock.patch.object(yo.os, "access", return_value=False) as fake_access:
                    with self.assertRaises(yo.YoError) as caught:
                        yo.require_kvm()
        self.assertIn("permissions", caught.exception.message)
        fake_access.assert_called_once_with("/dev/kvm", os.R_OK | os.W_OK)

    def test_linux_passes_when_the_device_is_present_and_accessible(self):
        with with_platform("linux"):
            with mock.patch.object(yo.os.path, "exists", return_value=True):
                with mock.patch.object(yo.os, "access", return_value=True):
                    yo.require_kvm()


if __name__ == "__main__":
    unittest.main()
