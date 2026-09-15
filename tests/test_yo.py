import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
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


class TestHerdrAliasBlock(FakeHome):
    def block(self):
        return yo.herdr_alias_block(
            {"Hostname": "127.0.0.1", "Port": "60022", "IdentityFile": "/Users/xiii/.lima/_config/user"}
        )

    def test_pins_the_yo_owned_known_hosts_file(self):
        self.assertIn('  UserKnownHostsFile "%s"' % yo.known_hosts_path(), self.block())

    def test_never_disables_strict_checking(self):
        block = self.block()
        self.assertNotIn("StrictHostKeyChecking no", block)
        self.assertNotIn("UserKnownHostsFile /dev/null", block)


class TestKnownHostsLines(unittest.TestCase):
    LIMA_VALUES = {"Hostname": "127.0.0.1", "Port": "60022", "IdentityFile": "x"}

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
        with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()

    def test_unparseable_lima_block_is_skipped_without_ssh(self):
        self.write_config(LIMA_BLOCK_MISSING_ALIAS_FIELDS)
        with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
            yo.ensure_guest_known_hosts()
        fake_ssh_run.assert_not_called()

    def test_fetches_via_operator_and_writes_the_known_hosts_file(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr="")
        with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()

        role, argv = fake_ssh_run.call_args[0]
        self.assertIs(role, yo.OPERATOR)
        self.assertEqual(argv[0], "sh")
        self.assertIn(yo.HOST_KEY_GLOB, argv[-1])

        with open(yo.known_hosts_path(), "r") as handle:
            content = handle.read()
        self.assertEqual(content, "[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc==\n")
        self.assertEqual(stat.S_IMODE(os.stat(yo.known_hosts_path()).st_mode), 0o600)

    def test_failed_fetch_raises_naming_the_cause(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(returncode=255, stdout="", stderr="ssh: connect to host 127.0.0.1 port 60022: Connection refused\n")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertIn("Connection refused", caught.exception.message)

    def test_no_parseable_keys_raises(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError):
                yo.ensure_guest_known_hosts()


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
        with mock.patch.object(yo, "err"):
            allowed = yo.aws_allowlist()
        self.assertIn(("default", "eu-west-1"), allowed)
        self.assertIn(("work", "us-east-1"), allowed)

    def test_sso_session_section_itself_is_excluded(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo, "err"):
            allowed = yo.aws_allowlist()
        self.assertNotIn("my-sso", [name for name, _ in allowed])

    def test_profile_without_sso_fields_is_excluded(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo, "err"):
            allowed = yo.aws_allowlist()
        self.assertNotIn("legacy", [name for name, _ in allowed])

    def test_profile_without_region_is_excluded_and_warned(self):
        self.write_aws_config(AWS_CONFIG_TEXT)
        with mock.patch.object(yo, "err") as fake_err:
            allowed = yo.aws_allowlist()
        self.assertNotIn("noregion", [name for name, _ in allowed])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("noregion", messages)

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

    def test_profile_name_with_shell_metacharacters_is_excluded_and_warned(self):
        self.write_aws_config(
            "[profile work; rm -rf /]\n"
            "sso_start_url = https://example.awsapps.com/start\n"
            "region = us-east-1\n"
        )
        with mock.patch.object(yo, "err") as fake_err:
            allowed = yo.aws_allowlist()
        self.assertEqual(allowed, [])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("work; rm -rf /", messages)


class TestOpProbeAlive(unittest.TestCase):
    def proc(self, returncode, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_exit_0_is_alive(self):
        self.assertTrue(yo.op_probe_alive(self.proc(0, "2048 SHA256:... user@host (RSA)\n")))

    def test_exit_1_no_identities_is_alive(self):
        self.assertTrue(yo.op_probe_alive(self.proc(1, "The agent has no identities.\n")))

    def test_exit_1_communication_failure_is_dead(self):
        self.assertFalse(
            yo.op_probe_alive(
                self.proc(1, "error fetching identities: communication with agent failed\n")
            )
        )

    def test_exit_2_is_dead(self):
        self.assertFalse(yo.op_probe_alive(self.proc(2, "Could not open a connection to your authentication agent.\n")))


class TestBrokerNeedsRestart(unittest.TestCase):
    def test_no_health_needs_restart(self):
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
        killed = []
        with mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"), mock.patch.object(
            yo, "aws_allowlist", return_value=[("work", "us-east-1")]
        ), mock.patch.object(yo, "ha_pid", return_value=555), mock.patch.object(
            yo, "aws_broker_health", return_value=health
        ), mock.patch.object(
            yo, "wait_for_pid_exit"
        ), mock.patch.object(
            yo, "start_aws_broker"
        ) as fake_start, mock.patch.object(
            yo, "push_aws_guest_files"
        ), mock.patch.object(
            yo.os, "kill", side_effect=lambda pid, sig: killed.append(pid)
        ):
            result = yo.ensure_aws_broker()
        self.assertTrue(result)
        self.assertEqual(killed, [111])
        fake_start.assert_called_once_with(555, ["work"])


class TestEnsureAwsBrokerDisabling(FakeHome):
    def test_no_aws_cli_stops_a_running_broker_and_clears_the_guest_config(self):
        with mock.patch.object(yo.shutil, "which", return_value=None), mock.patch.object(
            yo, "disable_aws_broker"
        ) as fake_disable:
            result = yo.ensure_aws_broker()
        self.assertFalse(result)
        fake_disable.assert_called_once_with()

    def test_no_sso_profiles_stops_a_running_broker_and_clears_the_guest_config(self):
        with mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"), mock.patch.object(
            yo, "aws_allowlist", return_value=[]
        ), mock.patch.object(yo, "disable_aws_broker") as fake_disable:
            result = yo.ensure_aws_broker()
        self.assertFalse(result)
        fake_disable.assert_called_once_with()

    def test_disable_kills_the_brokers_pid_and_removes_the_guest_config(self):
        killed = []
        removed = []

        def fake_ssh_run(role, argv, **kwargs):
            removed.append(list(argv))
            return mock.Mock(returncode=0)

        with mock.patch.object(
            yo, "aws_broker_health", return_value={"pid": 222, "watch_pid": 1, "allow": []}
        ), mock.patch.object(
            yo, "kill_stale_broker", side_effect=lambda pid: killed.append(pid)
        ), mock.patch.object(
            yo, "ssh_run", side_effect=fake_ssh_run
        ):
            yo.disable_aws_broker()
        self.assertEqual(killed, [222])
        self.assertEqual(removed, [["rm", "-f", yo.GUEST_AWS_CONFIG_PATH]])


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


class TestLimaConfigGaps(FakeHome):
    def mock_lima_list(self, forwards, host_agent_pid=123):
        payload = json.dumps({"hostAgentPID": host_agent_pid, "config": {"portForwards": forwards}})
        return mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=0, stdout=payload, stderr="")
        )

    def test_missing_instance_is_no_gaps(self):
        with mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="no such instance")
        ):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_present_reverse_rules_are_no_gaps(self):
        forwards = [
            {"guestSocket": yo.OP_GUEST_SOCK, "hostSocket": yo.op_sock(), "reverse": True},
            {
                "guestSocket": yo.AWS_BROKER_GUEST_SOCK,
                "hostSocket": yo.aws_broker_sock(),
                "reverse": True,
            },
        ]
        with self.mock_lima_list(forwards):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_missing_reverse_rule_is_reported_not_raised(self):
        with self.mock_lima_list([{"guestPort": 3773}]):
            gaps = yo.lima_config_gaps()
        self.assertEqual(set(gaps), {yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK})

    def test_only_one_missing_rule_is_reported(self):
        forwards = [{"guestSocket": yo.OP_GUEST_SOCK, "hostSocket": yo.op_sock(), "reverse": True}]
        with self.mock_lima_list(forwards):
            gaps = yo.lima_config_gaps()
        self.assertEqual(gaps, [yo.AWS_BROKER_GUEST_SOCK])

    def test_mismatched_host_socket_is_reported(self):
        forwards = [
            {"guestSocket": yo.OP_GUEST_SOCK, "hostSocket": "/somewhere/else.sock", "reverse": True},
            {
                "guestSocket": yo.AWS_BROKER_GUEST_SOCK,
                "hostSocket": yo.aws_broker_sock(),
                "reverse": True,
            },
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.lima_config_gaps()
        self.assertIn(yo.OP_GUEST_SOCK, gaps)

    def test_reads_host_agent_pid_from_the_same_listing(self):
        with self.mock_lima_list([], host_agent_pid=999):
            self.assertEqual(yo.ha_pid(), 999)

    def test_missing_instance_ha_pid_is_none(self):
        with mock.patch.object(yo, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="")):
            self.assertIsNone(yo.ha_pid())


class TestLimaConfigGapMessage(unittest.TestCase):
    def test_names_the_full_array_migration_command(self):
        message = yo.lima_config_gap_message([yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK])
        self.assertIn(".portForwards = ", message)
        self.assertIn(json.dumps(yo.LIMA_PORT_FORWARDS), message)
        self.assertIn(yo.OP_GUEST_SOCK, message)
        self.assertIn(yo.AWS_BROKER_GUEST_SOCK, message)


class TestVmUpNoticesLimaConfigGapsInsteadOfRefusing(FakeHome):
    def test_a_gap_is_reported_on_stderr_and_the_vm_still_comes_up(self):
        with mock.patch.object(yo, "lima_config_gaps", return_value=[yo.OP_GUEST_SOCK]), mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=0, stdout="yolobox\n", stderr="")
        ), mock.patch.object(yo, "ensure_agent_ssh_config"), mock.patch.object(
            yo, "vm_services"
        ) as fake_services, mock.patch.object(yo, "err") as fake_err:
            yo.vm_up()
        message = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn(yo.OP_GUEST_SOCK, message)
        fake_services.assert_called_once_with()

    def test_no_gaps_prints_nothing_about_lima_config(self):
        with mock.patch.object(yo, "lima_config_gaps", return_value=[]), mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=0, stdout="yolobox\n", stderr="")
        ), mock.patch.object(yo, "ensure_agent_ssh_config"), mock.patch.object(
            yo, "vm_services"
        ), mock.patch.object(yo, "err") as fake_err:
            yo.vm_up()
        fake_err.assert_not_called()


class TestHerdrMachineExists(unittest.TestCase):
    def test_empty_listing_is_false(self):
        self.assertFalse(yo.herdr_machine_exists("[]"))

    def test_matching_target_field_is_true(self):
        self.assertTrue(
            yo.herdr_machine_exists(
                '[{"id": "1", "label": "yolobox", "target": "yolobox", '
                '"session": null, "enabled": true}]'
            )
        )

    def test_unrelated_target_is_false(self):
        self.assertFalse(yo.herdr_machine_exists('[{"target": "other-box"}]'))

    def test_yolobox_value_outside_the_target_field_is_not_a_match(self):
        # Locks in the fix for a hidden failure: the old implementation matched
        # "yolobox" anywhere in an entry, so a machine merely *labelled*
        # yolobox but pointed at a different ssh target read as already added.
        self.assertFalse(yo.herdr_machine_exists('[{"label": "yolobox", "target": "other-box"}]'))

    def test_unparseable_json_raises(self):
        with self.assertRaises(yo.YoError):
            yo.herdr_machine_exists("not json")

    def test_non_array_json_raises(self):
        with self.assertRaises(yo.YoError):
            yo.herdr_machine_exists('{"target": "yolobox"}')


def yaml_scalar(value):
    value = value.strip().strip('"')
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


def portforwards_yaml_block(text):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "portForwards:":
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace():
            end = i
            break
    return lines[start:end]


def parse_yaml_list_entries(lines):
    entries = []
    current = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            stripped = stripped[2:].strip()
        if current is None or not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        current[key.strip()] = yaml_scalar(value)
    if current is not None:
        entries.append(current)
    return entries


def canonical_forwards(entries):
    return sorted(json.dumps(entry, sort_keys=True) for entry in entries)


class TestLimaYamlMatchesPortForwardsConstant(unittest.TestCase):
    def test_full_port_forwards_match_lima_port_forwards_constant(self):
        yaml_path = Path(yo.__file__).parent / "lima" / "yolobox.yaml"
        parsed = parse_yaml_list_entries(portforwards_yaml_block(yaml_path.read_text()))
        self.assertEqual(canonical_forwards(parsed), canonical_forwards(yo.LIMA_PORT_FORWARDS))


class TestAgentEnvMatchesYoSockets(unittest.TestCase):
    def test_agent_side_aws_broker_socket_matches_agent_env_nix(self):
        nix_path = Path(yo.__file__).parent / "nix" / "lib" / "agent-env.nix"
        text = nix_path.read_text()
        self.assertIn(yo.AGENT_AWS_BROKER_SOCK, text)

    def test_guest_aws_config_path_matches_agent_env_nix(self):
        nix_path = Path(yo.__file__).parent / "nix" / "lib" / "agent-env.nix"
        text = nix_path.read_text()
        suffix = yo.GUEST_AWS_CONFIG_PATH[len(yo.AGENT_HOME) :]
        self.assertEqual(yo.AGENT_HOME, "/home/agent")
        self.assertIn('"${agentHome}%s"' % suffix, text)


class TestBaseNixMatchesYoConstants(unittest.TestCase):
    def base_nix_text(self):
        return (Path(yo.__file__).parent / "nix" / "base.nix").read_text()

    def test_op_and_aws_broker_guest_sockets_compose_from_base_nix(self):
        text = self.base_nix_text()
        op_dir = "/run/yolobox-op"
        self.assertIn(op_dir, text)
        for guest_sock in (yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK):
            self.assertTrue(guest_sock.startswith(op_dir + "/"))
            target = guest_sock[len(op_dir + "/") :]
            self.assertIn('"%s"' % target, text)

    def test_proxy_socket_unit_names_appear_in_base_nix(self):
        text = self.base_nix_text()
        for unit in yo.PROXY_SOCKET_UNITS:
            self.assertTrue(unit.endswith(".socket"))
            name = unit[: -len(".socket")]
            self.assertIn(name, text)


class TestGcHelpNamesEveryBuildDir(unittest.TestCase):
    def test_gc_description_includes_every_build_dir(self):
        # Parse BUILD_DIRS from the shell script
        shell_path = Path(yo.__file__).parent / "nix" / "guest" / "yolobox-guest.sh"
        shell_text = shell_path.read_text()

        # Extract BUILD_DIRS array using regex: BUILD_DIRS=(node_modules target .next)
        match = re.search(r'BUILD_DIRS=\(([^)]+)\)', shell_text)
        self.assertIsNotNone(match, "BUILD_DIRS not found in shell script")

        # Parse the directory names
        build_dirs_str = match.group(1)
        build_dirs = build_dirs_str.split()
        self.assertTrue(build_dirs, "BUILD_DIRS appears to be empty")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            yo.build_parser().parse_args(["gc", "--help"])
        self.assertEqual(cm.exception.code, 0)

        for dir_name in build_dirs:
            self.assertIn(
                dir_name,
                buf.getvalue(),
                f"BUILD_DIRS entry '{dir_name}' not found in `yo gc --help`",
            )


class TestTailscaleStatusLine(unittest.TestCase):
    def probe(self, backend, serve):
        return json.dumps({"BackendState": backend}) + "\n\n---\n" + serve + "\n"

    def test_running_with_active_serve_is_healthy(self):
        line = yo.tailscale_status_line(self.probe("Running", "active"), "")
        self.assertEqual(line, "tailscale: Running, serve active")

    def test_not_logged_in_names_login_and_serve_restart(self):
        line = yo.tailscale_status_line(self.probe("NeedsLogin", "failed"), "")
        self.assertIn("WARNING NeedsLogin, serve failed", line)
        self.assertIn("yo ssh sudo tailscale up", line)
        self.assertIn("yo ssh sudo systemctl restart yolobox-tailscale-serve", line)

    def test_running_with_dead_serve_only_asks_for_serve_restart(self):
        line = yo.tailscale_status_line(self.probe("Running", "inactive"), "")
        self.assertIn("serve inactive", line)
        self.assertNotIn("tailscale up", line)

    def test_unparseable_output_surfaces_stderr(self):
        line = yo.tailscale_status_line("", "failed to connect to local tailscaled")
        self.assertIn("status unreadable: failed to connect to local tailscaled", line)

    def test_json_that_is_not_an_object_is_unreadable(self):
        for payload in ("null", "[]"):
            with self.subTest(payload=payload):
                line = yo.tailscale_status_line(payload + "\n\n---\nactive\n", "")
                self.assertIn("status unreadable: " + payload, line)

    def test_silent_failure_says_no_output(self):
        line = yo.tailscale_status_line("", "")
        self.assertIn("status unreadable: no output", line)

    def test_box_without_tailscale_says_to_bootstrap(self):
        self.assertIn("yo bootstrap", yo.tailscale_status_line("NO_TAILSCALE\n", ""))


class TestT3ServiceProblem(unittest.TestCase):
    def test_loaded_and_active_is_no_problem(self):
        self.assertIsNone(yo.t3_service_problem("loaded", "active"))

    def test_empty_load_state_means_agent_unreachable(self):
        self.assertIn("cannot reach", yo.t3_service_problem("", ""))

    def test_missing_unit_names_the_install_restart(self):
        self.assertIn("yolobox-harness-install", yo.t3_service_problem("not-found", "inactive"))

    def test_failed_unit_names_its_state_and_journal(self):
        problem = yo.t3_service_problem("loaded", "failed")
        self.assertIn("service is failed", problem)
        self.assertIn("journalctl --user -u t3code", problem)


class TestT3RequireService(unittest.TestCase):
    def require(self, unit_stdout, curl_returncode=0):
        def ssh_run(role, argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=unit_stdout, stderr="")

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, curl_returncode, stdout="", stderr="")

        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(
            yo, "ssh_run", ssh_run
        ), mock.patch.object(yo, "run", run):
            yo.t3_require_service()

    def test_unreachable_agent_is_refused(self):
        with self.assertRaisesRegex(yo.YoError, "cannot reach"):
            self.require("")

    def test_uninstalled_service_is_refused(self):
        with self.assertRaisesRegex(yo.YoError, "not installed"):
            self.require("not-found\ninactive\n")

    def test_failed_service_is_refused(self):
        with self.assertRaisesRegex(yo.YoError, "service is failed"):
            self.require("loaded\nfailed\n")

    def test_active_service_answering_on_the_mac_passes(self):
        self.require("loaded\nactive\n")

    def test_active_service_unreachable_from_the_mac_is_refused(self):
        with self.assertRaises(yo.YoError):
            self.require("loaded\nactive\n", curl_returncode=7)


class TestStatusWithVmDown(unittest.TestCase):
    def test_unreachable_vm_is_reported_once_and_no_guest_probe_runs(self):
        stdout = io.StringIO()
        guest_calls = []

        def ssh_run(role, argv, **kwargs):
            guest_calls.append(argv)
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="")

        with contextlib.redirect_stdout(stdout), mock.patch.object(
            yo, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
        ), mock.patch.object(
            yo, "account_status", lambda: ("unreachable", "")
        ), mock.patch.object(yo, "ssh_run", ssh_run), mock.patch.object(
            yo, "lima_config_gaps", lambda: []
        ), mock.patch.object(yo.shutil, "which", lambda name: None):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))

        self.assertEqual(guest_calls, [])
        self.assertEqual(stdout.getvalue().count("unreachable"), 1)
        self.assertIn("yo up", stdout.getvalue())


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
    def test_darwin_names_the_label_in_the_local_domain(self):
        with with_platform("darwin"):
            url = yo.pair_base_url("mymac")
        self.assertEqual(url, "http://mymac.local:3773")

    def test_linux_uses_the_primary_route_ip(self):
        with with_platform("linux"):
            with mock.patch.object(
                yo.socket, "socket", return_value=FakeRouteSocket(ip="192.168.1.42")
            ):
                with mock.patch.object(yo, "err") as fake_err:
                    url = yo.pair_base_url("box")
        self.assertEqual(url, "http://192.168.1.42:3773")
        fake_err.assert_called_once()

    def test_linux_refuses_naming_base_url_as_the_override_when_there_is_no_route(self):
        with with_platform("linux"):
            with mock.patch.object(
                yo.socket, "socket", return_value=FakeRouteSocket(connect_error=OSError("no route"))
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.pair_base_url("box")
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


ACTIVE_ZONES = "FedoraWorkstation (default)\n  interfaces: wlp5s0\ndocker\n  interfaces: docker0\n"


class TestFirewalldAdvisory(unittest.TestCase):
    def run_advisory(self, open_zones):
        queried = []

        def fake_run(argv, **kwargs):
            if argv[1] == "--state":
                return mock.Mock(returncode=0, stdout="running\n", stderr="")
            if argv[1] == "--get-active-zones":
                return mock.Mock(returncode=0, stdout=ACTIVE_ZONES, stderr="")
            zone = argv[1][len("--zone="):]
            queried.append(zone)
            return mock.Mock(returncode=0 if zone in open_zones else 1, stdout="", stderr="")

        with with_platform("linux"):
            with mock.patch.object(yo.shutil, "which", return_value="/usr/bin/firewall-cmd"):
                with mock.patch.object(yo, "run", side_effect=fake_run):
                    with mock.patch.object(yo, "err") as fake_err:
                        yo.firewalld_advisory()
        return queried, fake_err.call_args_list

    def test_queries_zone_names_without_their_annotations(self):
        queried, _ = self.run_advisory(open_zones=["FedoraWorkstation"])
        self.assertEqual(queried, ["FedoraWorkstation"])

    def test_silent_when_an_active_zone_opens_the_port(self):
        _, warnings = self.run_advisory(open_zones=["FedoraWorkstation"])
        self.assertEqual(warnings, [])

    def test_warns_naming_add_port_when_no_zone_opens_the_port(self):
        queried, warnings = self.run_advisory(open_zones=[])
        self.assertEqual(queried, ["FedoraWorkstation", "docker"])
        self.assertIn("--add-port=3773/tcp", " ".join(call.args[0] for call in warnings))

    def test_no_op_on_darwin(self):
        with with_platform("darwin"):
            with mock.patch.object(yo, "run") as fake_run:
                yo.firewalld_advisory()
        fake_run.assert_not_called()
