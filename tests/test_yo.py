import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
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


class TestRunToolMissing(unittest.TestCase):
    def test_a_missing_executable_is_a_yo_error_naming_it(self):
        with mock.patch.object(yo.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(yo.YoError) as caught:
                yo.run(["scp", "a", "b"])
        self.assertEqual(caught.exception.message, "scp not found on PATH")

    def test_exec_of_a_missing_executable_is_a_yo_error_naming_it(self):
        with (
            mock.patch.object(yo.signal, "signal"),
            mock.patch.object(yo.os, "execvpe", side_effect=FileNotFoundError),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.exec_process(["ssh", "host"])
        self.assertEqual(caught.exception.message, "ssh not found on PATH")


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

    def test_default_roots_refuse_tmp(self):
        with self.assertRaises(yo.YoError):
            yo.guest_path("/tmp/x")


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

    def test_absolute_guest_source_under_tmp_is_accepted(self):
        plan = yo.resolve_cp_operands([":/tmp/claude-1000/x", "dest"], self.MIRROR)
        self.assertEqual(plan.direction, "from_guest")
        self.assertEqual(plan.guest_paths, ["/tmp/claude-1000/x"])

    def test_tmp_itself_is_accepted(self):
        plan = yo.resolve_cp_operands(["a.txt", ":/tmp"], self.MIRROR)
        self.assertEqual(plan.guest_paths, ["/tmp"])

    def test_sibling_prefix_of_tmp_is_refused(self):
        with self.assertRaises(yo.YoError):
            yo.resolve_cp_operands([":/tmpx/y", "dest"], self.MIRROR)

    def test_tmp_traversal_out_of_tmp_is_refused(self):
        with self.assertRaises(yo.YoError):
            yo.resolve_cp_operands([":/tmp/../etc", "dest"], self.MIRROR)

    def test_relative_spelling_cannot_reach_tmp(self):
        with self.assertRaises(yo.YoError):
            yo.resolve_cp_operands([":../../../tmp", "dest"], self.MIRROR)

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
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(yo, "ssh_run", fake_ssh_run),
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
        with self.assertRaises(yo.YoError):
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
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(yo, "ssh_run", fake_ssh_run),
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
        self.assertEqual(calls["ssh_run"], [["true"]])
        self.assertIn("lima-yolobox:/home/agent/x", calls["run"])


class TestCpFromGuestNeverWritesThroughSymlinks(CpHarness):
    """scp is faked by a function that writes files into whatever directory
    cmd_cp told it to download into, like the real one would."""

    def setUp(self):
        super().setUp()
        self.scp_destinations = []

    def copy(self, paths, guest_files, scp_fails=False):
        # guest_files: {relative path inside the download dir: content}
        def fake_run(argv, **kwargs):
            destination = argv[-1]
            self.scp_destinations.append(destination)
            if scp_fails:
                raise yo.YoError("scp failed")
            for relative, content in guest_files.items():
                path = os.path.join(destination, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write(content)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(
                yo,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ),
        ):
            yo.cmd_cp(yo.build_parser().parse_args(["cp"] + paths))

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def leftovers(self, directory):
        return [n for n in os.listdir(directory) if n.startswith(".yo-cp-")]

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

    def test_nothing_lands_when_any_entry_is_refused(self):
        os.makedirs("dest/proj")
        os.symlink("/etc/hosts", "dest/proj/b")

        with self.assertRaises(yo.YoError):
            self.copy([":~/proj", "dest/"], {"proj/a": "1", "proj/b": "2"})

        self.assertFalse(os.path.lexists("dest/proj/a"))

    def test_a_top_level_symlink_at_the_destination_is_refused(self):
        os.makedirs("elsewhere")
        os.makedirs("dest")
        os.symlink(os.path.join(self.cwd, "elsewhere"), "dest/proj")

        with self.assertRaises(yo.YoError):
            self.copy([":~/proj", "dest/"], {"proj/f": "x"})

        self.assertEqual(os.listdir("elsewhere"), [])

    def test_a_symlink_as_the_named_destination_file_is_refused(self):
        Path("victim").write_text("precious")
        os.symlink(os.path.join(self.cwd, "victim"), "renamed.txt")

        with self.assertRaises(yo.YoError):
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

        with self.assertRaises(yo.YoError):
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

        with self.assertRaises(yo.YoError):
            self.copy([":~/x", "dest/"], {}, scp_fails=True)

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
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(
                yo,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ),
            self.assertRaises(yo.YoError),
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
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(
                yo,
                "ssh_run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
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
            mock.patch.object(yo, "remove_staging", side_effect=yo.YoError("stuck")),
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

        with self.assertRaises(yo.YoError):
            self.copy([":~/x", "afile/"], {"x": "data"})

        self.assertEqual(self.read("afile"), "keep")

    def test_a_missing_destination_parent_is_a_yo_error(self):
        with self.assertRaises(yo.YoError) as caught:
            self.copy([":~/x", "no/such/parent"], {"x": "data"})

        self.assertIn("no/such", caught.exception.message)


STALE_REMOTE = "ssh://lima-yolobox/home/alice.guest/wrk/rune"


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
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo, "run", fake_run),
            mock.patch.object(yo, "ssh_run", fake_ssh_run),
            self.assertRaises(yo.YoError),
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

    def test_a_directory_without_symlinks_is_uploaded(self):
        os.makedirs("repo/sub")
        Path("repo/sub/f").write_text("x")
        argv = self.run_cp(["repo", ":~/x"])["run"]
        self.assertIn(os.path.join(self.cwd, "repo"), argv)

    def test_a_symlink_on_the_guest_side_is_not_checked(self):
        os.symlink("a.txt", "link")
        self.run_cp([":~/x", "link2"])


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
        patcher = mock.patch.object(
            yo, "require_agent_account", mock.Mock(return_value="")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def link(self, get_url):
        calls = []

        def fake_run(argv, **rest):
            argv = list(argv)
            calls.append(argv)
            if "get-url" in argv:
                return get_url
            if "--show-toplevel" in argv:
                return mock.Mock(returncode=0, stdout=self.project + "\n", stderr="")
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


class TestLogicalToplevel(FakeHome):
    def make(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def link(self, target, name):
        os.symlink(target, os.path.join(self.home, name))

    def test_a_symlinked_spelling_of_the_root_is_kept(self):
        self.make("Developer", "proj", "sub")
        self.link(os.path.join(self.home, "Developer"), "wrk")
        top = os.path.realpath(os.path.join(self.home, "Developer", "proj"))
        cwd = os.path.join(self.home, "wrk", "proj", "sub")
        self.assertEqual(
            yo.logical_toplevel(top, cwd), os.path.join(self.home, "wrk", "proj")
        )

    def test_the_root_itself_as_cwd(self):
        proj = self.make("proj")
        self.assertEqual(yo.logical_toplevel(os.path.realpath(proj), proj), proj)

    def test_a_symlink_below_the_root_does_not_shift_the_root(self):
        # cwd is p/a/ln, which resolves to p/b/c/d: three levels below the
        # root physically, two logically.
        self.make("p", "b", "c", "d")
        self.make("p", "a")
        os.symlink("../b/c/d", os.path.join(self.home, "p", "a", "ln"))
        top = os.path.realpath(os.path.join(self.home, "p"))
        cwd = os.path.join(self.home, "p", "a", "ln")
        self.assertEqual(yo.logical_toplevel(top, cwd), os.path.join(self.home, "p"))

    def test_a_root_with_no_logical_ancestor_falls_back_to_the_physical_path(self):
        self.make("p", "sub")
        self.link(os.path.join(self.home, "p", "sub"), "shortcut")
        top = os.path.realpath(os.path.join(self.home, "p"))
        cwd = os.path.join(self.home, "shortcut")
        self.assertEqual(yo.logical_toplevel(top, cwd), top)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_matches_what_git_reports_from_inside_a_symlinked_subdirectory(self):
        self.make("p", "b", "c", "d")
        self.make("p", "a")
        os.symlink("../b/c/d", os.path.join(self.home, "p", "a", "ln"))
        subprocess.run(["git", "init", "-q", os.path.join(self.home, "p")], check=True)
        previous = os.getcwd()
        os.chdir(os.path.join(self.home, "p", "a", "ln"))
        self.addCleanup(os.chdir, previous)
        reported = yo.run(["git", "rev-parse", "--show-toplevel"], check=True)
        cwd = os.path.join(self.home, "p", "a", "ln")
        self.assertEqual(
            yo.logical_toplevel(reported.stdout.strip(), cwd),
            os.path.join(self.home, "p"),
        )


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
        self.assertEqual(
            self.read_config(), yo.INCLUDE_LINE + "\nHost other\n  User x\n"
        )

    def test_accepted_creates_ssh_dir_and_config_when_absent(self):
        with isatty(True):
            with mock.patch("builtins.input", return_value="yes"):
                yo.ensure_ssh_config_include(refuse=True)
        ssh_dir = os.path.join(self.home, ".ssh")
        self.assertEqual(stat.S_IMODE(os.stat(ssh_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path()).st_mode), 0o600)
        self.assertEqual(self.read_config(), yo.INCLUDE_LINE + "\n")

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
            self.assertEqual(handle.read(), yo.INCLUDE_LINE + "\nHost other\n")

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
                handle.read(), yo.INCLUDE_LINE.encode() + b"\n# caf\xe9\nHost other\n"
            )

    def test_an_unreadable_config_is_an_error_not_an_empty_file(self):
        path = self.write_config("Host other\n")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        with isatty(True):
            with mock.patch("builtins.input", return_value="y"):
                with self.assertRaises(yo.YoError) as raised:
                    yo.ensure_ssh_config_include(refuse=True)
        self.assertIn(
            "cannot read %s" % os.path.realpath(path), raised.exception.message
        )
        os.chmod(path, 0o600)
        with open(path) as handle:
            self.assertEqual(handle.read(), "Host other\n")

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

    def test_end_of_input_at_the_prompt_is_a_no(self):
        original = "Host other\n"
        self.write_config(original)
        with isatty(True):
            with mock.patch("builtins.input", side_effect=EOFError):
                with self.assertRaises(yo.YoError):
                    yo.ensure_ssh_config_include(refuse=True)
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
    '  IdentityFile "/Users/alice/.lima/_config/user"\n'
    "  StrictHostKeyChecking no\n"
    "  User alice\n"
    "  ControlMaster auto\n"
    '  ControlPath "/Users/alice/.lima/yolobox/ssh.sock"\n'
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
    def test_agent_role_never_forwards_even_if_a_later_host_block_says_yes(self):
        # A user's own "Host lima-yolobox / ForwardAgent yes" sits after the
        # Include; ssh keeps the first value, so the owned block must win.
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
        self.assertEqual(forwardagent("alice"), ["forwardagent yes"])

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
        self.assertIn('  IdentityFile "/Users/alice/.lima/_config/user"\n', alias)

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


class TestHerdrAliasBlock(FakeHome):
    def block(self):
        return yo.herdr_alias_block(
            {
                "Hostname": "127.0.0.1",
                "Port": "60022",
                "IdentityFile": "/Users/alice/.lima/_config/user",
            }
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


LIMA_ALIAS_BLOCK = (
    "Host lima-yolobox\n"
    "  StrictHostKeyChecking no\n"
    "  UserKnownHostsFile /dev/null\n"
    "  NoHostAuthenticationForLocalhost yes\n"
    "  User alice\n"
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
        with mock.patch.object(yo, "err"):
            yo.ensure_agent_ssh_config()

    def effective(self, argv):
        proc = subprocess.run(
            list(argv) + ["-G", yo.VM_HOST], capture_output=True, text=True, check=True
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
                self.assert_pinned(self.effective(yo.ssh_base(role)))

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
        effective = self.effective(yo.ssh_base(yo.OPERATOR_FIRST_CONTACT))
        self.assertEqual(effective["stricthostkeychecking"], "false")
        self.assertEqual(effective["userknownhostsfile"], "/dev/null")
        self.assertEqual(effective["nohostauthenticationforlocalhost"], "yes")

    def test_the_push_channel_takes_its_pin_from_the_file_alone(self):
        project = os.path.join(self.home, "wrk", "rune")
        os.makedirs(project)
        calls = []

        def fake_run(argv, **rest):
            calls.append(list(argv))
            missing = "get-url" in argv
            top = project if "--show-toplevel" in argv else ""
            return mock.Mock(returncode=128 if missing else 0, stdout=top, stderr="")

        with (
            mock.patch.object(yo, "require_agent_account"),
            mock.patch.object(yo, "logical_cwd", lambda: project),
            mock.patch.object(yo, "guest_helper_check"),
            mock.patch.object(yo, "guest_helper"),
            mock.patch.object(yo, "run", fake_run),
        ):
            yo.cmd_link(yo.build_parser().parse_args(["link"]))
        ssh_command = [c for c in calls if "remote.yolobox.sshCommand" in c][0][-1]
        self.assertEqual(
            shlex.split(ssh_command),
            ["ssh", "-F", yo.ssh_cfg(), "-o", "ControlPath=none"],
        )


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
        proc = mock.Mock(
            returncode=0, stdout="ssh-ed25519 AAAAC3abc== root@yolobox\n", stderr=""
        )
        with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
            yo.ensure_guest_known_hosts()

        role, argv = fake_ssh_run.call_args[0]
        self.assertIs(role, yo.OPERATOR_FIRST_CONTACT)
        self.assertEqual(argv[0], "sh")
        self.assertIn(yo.HOST_KEY_GLOB, argv[-1])

        with open(yo.known_hosts_path(), "r") as handle:
            content = handle.read()
        self.assertEqual(content, "[127.0.0.1]:60022 ssh-ed25519 AAAAC3abc==\n")
        self.assertEqual(stat.S_IMODE(os.stat(yo.known_hosts_path()).st_mode), 0o600)

    def test_failed_fetch_raises_naming_the_cause(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(
            returncode=255,
            stdout="",
            stderr="ssh: connect to host 127.0.0.1 port 60022: Connection refused\n",
        )
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertIn("Connection refused", caught.exception.message)
        # main() adds the "yo: " prefix itself.
        self.assertFalse(caught.exception.message.startswith("yo:"))

    def test_no_parseable_keys_raises(self):
        self.write_config(LIMA_BLOCK)
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(yo, "ssh_run", return_value=proc):
            with self.assertRaises(yo.YoError) as caught:
                yo.ensure_guest_known_hosts()
        self.assertFalse(caught.exception.message.startswith("yo:"))


class TestParsePairingUrl(unittest.TestCase):
    def test_accepts_loopback(self):
        url = "http://127.0.0.1:3773/?token=x"
        self.assertEqual(yo.parse_pairing_url("  Pairing URL: %s\n" % url), url)

    def test_rejects_anything_but_loopback_http(self):
        urls = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://evil.example/",
            "http://evil.com\\@127.0.0.1:3773/x",
            "http://127.0.0.1.evil.com:3773/x",
            "http://127.0.0.1:3774/x",
            "http://127.0.0.1/x",
            "http://user@127.0.0.1:3773/x",
            "http://127.0.0.1:3773",
            "http://127.0.0.1:3773/x y",
            "http://127.0.0.1:3773/x\x1b[31m",
            "http://127.0.0.1:3773/x\u009by",
            "http://[::1/pair",
            "http://127.0.0.1:3773\uff03/x",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(yo.YoError):
                    yo.parse_pairing_url("Pairing URL: %s\n" % url)

    def test_accepts_localhost_and_https(self):
        for url in ("http://localhost:3773/p#t=1", "https://127.0.0.1:3773/p"):
            with self.subTest(url=url):
                self.assertEqual(yo.parse_pairing_url("Pairing URL: %s\n" % url), url)

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

    def allowed_for(self, section, region="us-east-1"):
        self.write_aws_config(
            "[%s]\nsso_start_url = https://example.awsapps.com/start\nregion = %s\n"
            % (section, region)
        )
        with mock.patch.object(yo, "err") as fake_err:
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
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(yo, "ha_pid", return_value=555),
            mock.patch.object(yo, "aws_broker_health", return_value=health),
            mock.patch.object(yo, "wait_for_pid_exit"),
            mock.patch.object(yo, "start_aws_broker") as fake_start,
            mock.patch.object(yo, "push_aws_guest_files"),
            mock.patch.object(
                yo.os, "kill", side_effect=lambda pid, sig: killed.append(pid)
            ),
        ):
            result = yo.ensure_aws_broker()
        self.assertTrue(result)
        self.assertEqual(killed, [111])
        fake_start.assert_called_once_with(555, ["work"])


class TestStaleBrokerPidIsValidated(FakeHome):
    def test_a_pid_that_is_not_a_real_pid_is_never_signalled(self):
        for value in BAD_PIDS:
            with self.subTest(value=value):
                health = {"pid": value, "watch_pid": 999, "allow": []}
                with mock.patch.object(yo.os, "kill") as fake_kill:
                    yo.kill_broker_in(health)
                fake_kill.assert_not_called()


class TestStartAwsBrokerFiles(FakeHome):
    def start(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 0
        with (
            mock.patch.object(yo.subprocess, "Popen", return_value=proc),
            mock.patch.object(yo, "read_handshake", return_value=["READY"]),
        ):
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


class TestEnsureAwsBrokerDisabling(FakeHome):
    def test_no_aws_cli_stops_a_running_broker_and_clears_the_guest_config(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo, "disable_aws_broker") as fake_disable,
        ):
            result = yo.ensure_aws_broker()
        self.assertFalse(result)
        fake_disable.assert_called_once_with()

    def test_no_sso_profiles_stops_a_running_broker_and_clears_the_guest_config(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(yo, "aws_allowlist", return_value=[]),
            mock.patch.object(yo, "disable_aws_broker") as fake_disable,
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
                yo,
                "aws_broker_health",
                return_value={"pid": 222, "watch_pid": 1, "allow": []},
            ),
            mock.patch.object(
                yo, "kill_stale_broker", side_effect=lambda pid: killed.append(pid)
            ),
            mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run),
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
    def mock_lima_list(
        self,
        forwards,
        host_agent_pid=123,
        mounts=None,
        forward_agent=None,
        over_vsock=False,
    ):
        config = {"portForwards": forwards}
        if mounts is not None:
            config["mounts"] = mounts
        config["ssh"] = {}
        if forward_agent is not None:
            config["ssh"]["forwardAgent"] = forward_agent
        if over_vsock is not None:
            config["ssh"]["overVsock"] = over_vsock
        payload = json.dumps({"hostAgentPID": host_agent_pid, "config": config})
        return mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=0, stdout=payload, stderr="")
        )

    def test_missing_instance_is_no_gaps(self):
        with mock.patch.object(
            yo,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="no such instance"),
        ):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_present_reverse_rules_are_no_gaps(self):
        with self.mock_lima_list(self.both_forwards()):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_an_instance_whose_t3_rule_predates_proto_tcp_is_no_gap(self):
        # Only the reverse socket rules are checked; an older instance's
        # {"guestPort": 3773, "hostIP": "0.0.0.0"} must not trigger a note.
        forwards = [{"guestPort": yo.T3_PORT, "hostIP": "0.0.0.0"}]
        with self.mock_lima_list(forwards + self.both_forwards()):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_missing_reverse_rule_is_reported_not_raised(self):
        forwards = [self.both_forwards()[0], {"guestPort": 3773}]
        with self.mock_lima_list(forwards):
            gaps = yo.lima_config_gaps()
        self.assertEqual(set(gaps), {yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK})

    def test_only_one_missing_rule_is_reported(self):
        forwards = [
            forward
            for forward in self.both_forwards()
            if forward.get("guestSocket") != yo.AWS_BROKER_GUEST_SOCK
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.lima_config_gaps()
        self.assertEqual(gaps, [yo.AWS_BROKER_GUEST_SOCK])

    def test_mismatched_host_socket_is_reported(self):
        forwards = [
            {
                "guestSocket": yo.OP_GUEST_SOCK,
                "hostSocket": "/somewhere/else.sock",
                "reverse": True,
            },
            *self.both_forwards()[2:],
        ]
        with self.mock_lima_list(forwards):
            gaps = yo.lima_config_gaps()
        self.assertIn(yo.OP_GUEST_SOCK, gaps)

    def test_an_instance_that_only_ignores_dhcp_still_mirrors_udp_listeners(self):
        # Older instances ignore UDP 68 alone; tailscaled's 41641 is still
        # mirrored onto the Mac, where the Mac's own Tailscale holds it.
        dhcp_only = self.udp_ignore_rule(
            guestPortRange=[68, 68], guestIPMustBeZero=True
        )
        with self.mock_lima_list([dhcp_only, *self.both_forwards()[1:]]):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_with_lima_default_guest_ip_still_mirrors_wildcard_listeners(
        self,
    ):
        # Lima skips an ignore rule for a listener on 0.0.0.0 or :: unless the
        # rule's guestIP is unspecified: tailscaled's 41641 and DHCP's 68 stay.
        loopback_only = self.udp_ignore_rule(guestIP="127.0.0.1")
        with self.mock_lima_list([loopback_only, *self.both_forwards()[1:]]):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_pinned_to_exactly_zero_still_mirrors_loopback_listeners(
        self,
    ):
        # guestIPMustBeZero makes 0.0.0.0 an exact match, so 127.0.0.1 and
        # specific-address listeners fall through to the default forward.
        exact_zero = self.udp_ignore_rule(guestIPMustBeZero=True)
        with self.mock_lima_list([exact_zero, *self.both_forwards()[1:]]):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_a_udp_ignore_rule_with_garbage_guest_ip_is_a_gap(self):
        for guest_ip in ("lizard", "", None, 0):
            with self.subTest(guest_ip=guest_ip):
                rule = self.udp_ignore_rule(guestIP=guest_ip)
                with self.mock_lima_list([rule, *self.both_forwards()[1:]]):
                    self.assertEqual(yo.lima_config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_an_ipv6_unspecified_udp_ignore_rule_covers_every_listener(self):
        rule = self.udp_ignore_rule(guestIP="::")
        with self.mock_lima_list([rule, *self.both_forwards()[1:]]):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_a_blanket_udp_rule_that_forwards_instead_of_ignoring_is_a_gap(self):
        forwarding = self.udp_ignore_rule(ignore=False)
        with self.mock_lima_list([forwarding, *self.both_forwards()[1:]]):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_UDP_FORWARDS])

    def test_reads_host_agent_pid_from_the_same_listing(self):
        with self.mock_lima_list([], host_agent_pid=999):
            self.assertEqual(yo.ha_pid(), 999)

    def test_a_host_agent_pid_that_is_not_a_real_pid_is_none(self):
        for value in BAD_PIDS:
            with self.subTest(value=value):
                with self.mock_lima_list([], host_agent_pid=value):
                    self.assertIsNone(yo.ha_pid())

    def test_a_mount_is_reported(self):
        mounts = [{"location": "/Users/x", "writable": True}]
        with self.mock_lima_list(self.both_forwards(), mounts=mounts):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_MOUNTS])

    def test_an_empty_mount_list_is_no_gap(self):
        with self.mock_lima_list(self.both_forwards(), mounts=[]):
            self.assertEqual(yo.lima_config_gaps(), [])

    def test_forward_agent_is_reported(self):
        with self.mock_lima_list(self.both_forwards(), forward_agent=True):
            self.assertEqual(yo.lima_config_gaps(), [yo.GAP_FORWARD_AGENT])

    def test_forward_agent_false_or_a_string_is_no_gap(self):
        for value in (False, "true"):
            with self.subTest(value=value):
                with self.mock_lima_list(self.both_forwards(), forward_agent=value):
                    self.assertEqual(yo.lima_config_gaps(), [])

    def test_ssh_over_vsock_left_at_lima_default_is_reported(self):
        for value in (None, True):
            with self.subTest(value=value):
                with self.mock_lima_list(self.both_forwards(), over_vsock=value):
                    self.assertEqual(yo.lima_config_gaps(), [yo.GAP_SSH_OVER_VSOCK])

    def test_the_over_vsock_remedy_sets_it_false(self):
        message = yo.lima_config_gap_message([yo.GAP_SSH_OVER_VSOCK])
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

    def both_forwards(self):
        # The instance's normalized UDP rule: lima fills in the full range.
        return [
            self.udp_ignore_rule(),
            {
                "guestSocket": yo.OP_GUEST_SOCK,
                "hostSocket": yo.op_sock(),
                "reverse": True,
            },
            {
                "guestSocket": yo.AWS_BROKER_GUEST_SOCK,
                "hostSocket": yo.aws_broker_sock(),
                "reverse": True,
            },
        ]

    def test_missing_instance_ha_pid_is_none(self):
        with mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="")
        ):
            self.assertIsNone(yo.ha_pid())


class TestLimaConfigGapMessage(unittest.TestCase):
    def test_names_the_full_array_migration_command(self):
        message = yo.lima_config_gap_message(
            [yo.OP_GUEST_SOCK, yo.AWS_BROKER_GUEST_SOCK]
        )
        self.assertIn(".portForwards = ", message)
        self.assertIn(json.dumps(yo.LIMA_PORT_FORWARDS), message)
        self.assertIn(yo.OP_GUEST_SOCK, message)
        self.assertIn(yo.AWS_BROKER_GUEST_SOCK, message)


class TestLimaSettingGapMessage(unittest.TestCase):
    def test_mounts_are_remedied_by_editing_mounts_alone(self):
        message = yo.lima_config_gap_message([yo.GAP_MOUNTS])
        self.assertIn("--set '.mounts = []'", message)
        self.assertNotIn("portForwards", message)

    def test_forward_agent_is_remedied_by_editing_forward_agent_alone(self):
        message = yo.lima_config_gap_message([yo.GAP_FORWARD_AGENT])
        self.assertIn("--set '.ssh.forwardAgent = false'", message)
        self.assertNotIn("portForwards", message)

    def test_the_udp_gap_is_remedied_by_restating_the_full_array(self):
        message = yo.lima_config_gap_message([yo.GAP_UDP_FORWARDS])
        self.assertIn(yo.RESTATE_PORT_FORWARDS, message)
        self.assertIn("41641", message)

    def test_a_socket_gap_and_the_udp_gap_print_one_restate(self):
        message = yo.lima_config_gap_message([yo.OP_GUEST_SOCK, yo.GAP_UDP_FORWARDS])
        self.assertEqual(message.count(".portForwards = "), 1)

    def test_every_gap_kind_gets_its_own_remedy(self):
        message = yo.lima_config_gap_message(
            [yo.OP_GUEST_SOCK, yo.GAP_MOUNTS, yo.GAP_FORWARD_AGENT]
        )
        for needle in (
            yo.OP_GUEST_SOCK,
            ".portForwards = ",
            ".mounts = []",
            ".ssh.forwardAgent = false",
        ):
            self.assertIn(needle, message)


class TestVmUpNoticesLimaConfigGapsInsteadOfRefusing(FakeHome):
    def vm_up_on_a_running_box(self, gaps):
        running = yo.LimaInfo(
            host_agent_pid=1,
            port_forwards=[],
            mounts=[],
            forward_agent=False,
            status="Running",
            over_vsock=False,
        )
        with (
            mock.patch.object(yo, "lima_config_gaps", return_value=gaps),
            mock.patch.object(yo, "lima_instance_info", return_value=running),
            mock.patch.object(yo, "stage2_up", return_value=True),
            mock.patch.object(yo, "ensure_agent_ssh_config"),
            mock.patch.object(yo, "ensure_guest_known_hosts"),
            mock.patch.object(yo, "vm_services") as fake_services,
            mock.patch.object(yo, "err") as fake_err,
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
        self.assertFalse(
            yo.herdr_machine_exists('[{"label": "yolobox", "target": "other-box"}]')
        )

    def test_unparseable_json_raises(self):
        with self.assertRaises(yo.YoError):
            yo.herdr_machine_exists("not json")

    def test_non_array_json_raises(self):
        with self.assertRaises(yo.YoError):
            yo.herdr_machine_exists('{"target": "yolobox"}')


class TestYoVersionLiteral(unittest.TestCase):
    # nix/pkgs/yolobox.nix (--replace-fail) and homebrew/yolobox.rb (inreplace)
    # both patch this exact line; if it is reformatted the brew build ships a
    # binary that still reports "dev" without failing.
    def test_yo_has_exactly_one_line_that_is_the_dev_version_literal(self):
        lines = Path(yo.__file__).read_text().splitlines()
        self.assertEqual(lines.count('YO_VERSION = "dev"'), 1)


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
        self.assertIn(
            "yolobox-harness-install", yo.t3_service_problem("not-found", "inactive")
        )

    def test_failed_unit_names_its_state_and_journal(self):
        problem = yo.t3_service_problem("loaded", "failed")
        self.assertIn("service is failed", problem)
        self.assertIn("journalctl --user -u t3code", problem)

    def test_failed_unit_names_the_stale_marker_recovery(self):
        problem = yo.t3_service_problem("loaded", "failed")
        self.assertIn(
            "systemctl --user reset-failed t3code.service && ~/.local/bin/t3 service install",
            problem,
        )

    def test_non_failed_states_do_not_suggest_reset_failed(self):
        for state in ("activating", "inactive"):
            with self.subTest(state=state):
                self.assertNotIn("reset-failed", yo.t3_service_problem("loaded", state))


class TestT3RequireService(unittest.TestCase):
    def require(self, unit_stdout, curl_returncode=0):
        def ssh_run(role, argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=unit_stdout, stderr="")

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, curl_returncode, stdout="", stderr=""
            )

        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(yo, "ssh_run", ssh_run),
            mock.patch.object(yo, "run", run),
        ):
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

        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(
                yo, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
            ),
            mock.patch.object(yo, "account_status", lambda: ("unreachable", "")),
            mock.patch.object(yo, "ssh_run", ssh_run),
            mock.patch.object(yo, "lima_config_gaps", lambda: []),
            mock.patch.object(yo, "initrd_waiting", lambda: False),
            mock.patch.object(yo.shutil, "which", lambda name: None),
        ):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))

        self.assertEqual(guest_calls, [])
        self.assertEqual(stdout.getvalue().count("unreachable"), 1)
        self.assertIn("yo up", stdout.getvalue())


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


if __name__ == "__main__":
    unittest.main()
