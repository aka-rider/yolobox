import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_yo import (
    FIXTURE_EMPTY_TABLE,
    FIXTURES,
    FakeHome,
    Reached,
    df_output,
    initrd_probe,
    yo,
)


@contextlib.contextmanager
def env_without(*names):
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in names:
            os.environ.pop(name, None)
        yield


class TestFlakeRef(FakeHome):
    def remember(self, ref):
        os.makedirs(os.path.dirname(yo.flake_ref_file()), exist_ok=True)
        with open(yo.flake_ref_file(), "w") as handle:
            handle.write(ref)

    def test_dev_build_without_a_version_refuses_with_no_pinned_flake(self):
        with mock.patch.object(yo.config, "YO_VERSION", "dev"):
            with self.assertRaises(yo.YoError) as caught:
                yo.flake_ref(None)
        self.assertIn("--version", caught.exception.message)

    def test_released_build_without_a_version_pins_its_own_tag(self):
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            self.assertEqual(yo.flake_ref(None), "github:aka-rider/yolobox/v1.2.3")

    def test_a_release_is_pinned_with_or_without_the_v(self):
        for version in ("1.7.2", "v1.7.2"):
            with self.subTest(version=version):
                self.assertEqual(
                    yo.flake_ref(version), "github:aka-rider/yolobox/v1.7.2"
                )

    def test_a_branch_or_tag_is_taken_from_the_upstream_repo(self):
        for ref in ("main", "my-branch", "v1.7.2-rc1", "2026-fix"):
            with self.subTest(ref=ref):
                self.assertEqual(yo.flake_ref(ref), "github:aka-rider/yolobox/" + ref)

    def test_a_github_flake_ref_with_owner_repo_and_ref_is_taken_verbatim(self):
        for ref in (
            "github:x/y/z",
            "github:you/yolobox/my-branch",
            "github:a.b/c_d/v1.2",
        ):
            with self.subTest(ref=ref):
                self.assertEqual(yo.flake_ref(ref), ref)

    def test_a_version_that_cannot_be_a_ref_is_refused(self):
        for version, reason in (
            ("", "--version must be"),
            ("1.7", "a release is X.Y.Z"),
            ("v2", "a release is X.Y.Z"),
            ("latest", "there is no latest ref"),
            ("~/yolobox", "--version must be"),
            ("./yolobox", "--version must be"),
            ("a b", "--version must be"),
            ("-x", "--version must be"),
        ):
            with self.subTest(version=version):
                with self.assertRaisesRegex(yo.YoError, reason):
                    yo.flake_ref(version)

    def test_a_ref_the_agent_could_control_is_refused(self):
        # The box runs `sudo nixos-rebuild --flake <ref>#yolobox`, and the agent
        # owns every path in the guest and can push to any checkout there.
        for ref in (
            "/home/agent/yolobox",
            "path:/home/agent/yolobox",
            "file:///home/agent/yolobox",
            "git+file:///home/agent/yolobox",
            "git+ssh://host/x/y",
            "github:x/y",
            "github:x",
            "github:x/y/z/w",
            "github:x/y/",
            "github://y/z",
            "github:x/y/-z",
            "github:x/y/z#evil",
            "github:x/y/z?dir=sub",
            "github:x/y/z\n",
            "github:x y/z/w",
            "main#x",
            "my/branch",
            "main\n",
            "1.7.2\n",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(yo.YoError, "--version must be|release is"):
                    yo.flake_ref(ref)

    def test_a_sharp_in_the_version_is_a_usage_error_not_a_second_attribute(self):
        with self.assertRaisesRegex(yo.YoError, "--version must be"):
            yo.flake_ref("v1.7.2#x")

    def test_the_hint_quotes_the_ref(self):
        self.remember("github:you/yolobox/x")
        hint = yo.rebuild_hint()
        flake_arg = hint.split("--flake ", 1)[1]
        self.assertEqual(shlex.split(flake_arg), ["github:you/yolobox/x#yolobox"])

    def test_the_hint_names_the_host_os_and_hostname_beside_the_username(self):
        self.remember("github:x/y/z")
        self.assertIn(
            "sudo YOLOBOX_USERNAME=$(id -un) YOLOBOX_HOST_OS=darwin "
            "YOLOBOX_HOSTNAME=yolobox nixos-rebuild",
            yo.rebuild_hint(),
        )

    def test_dev_build_falls_back_to_a_hint_naming_version_flag(self):
        with mock.patch.object(yo.config, "YO_VERSION", "dev"):
            with mock.patch.object(yo.ui, "err") as fake_err:
                hint = yo.rebuild_hint()
        self.assertIn("--version", hint)
        fake_err.assert_called_once()
        self.assertIn("--version", fake_err.call_args[0][0])

    def test_a_remembered_ref_beats_the_release(self):
        self.remember("github:you/yolobox/x\n")
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            hint = yo.rebuild_hint()
        self.assertIn("github:you/yolobox/x#yolobox", hint)
        self.assertNotIn("1.2.3", hint)

    def test_a_remembered_ref_that_is_no_longer_accepted_is_left_out_and_named(self):
        # An older yo remembered whatever it was given, such as a checkout path.
        for stored in (
            "/home/agent/yolobox",
            "path:/home/agent/yolobox",
            "github:x/y'; touch pwned #",
        ):
            with self.subTest(stored=stored):
                self.remember(stored)
                with (
                    mock.patch.object(yo.config, "YO_VERSION", "1.2.3"),
                    mock.patch.object(yo.ui, "err") as fake_err,
                ):
                    hint = yo.rebuild_hint()
                self.assertIn("github:aka-rider/yolobox/v1.2.3#yolobox", hint)
                self.assertNotIn("pwned", hint)
                self.assertNotIn("/home/agent", hint)
                fake_err.assert_called_once()
                self.assertIn(yo.flake_ref_file(), fake_err.call_args.args[0])
                self.assertIn(
                    "no longer an accepted flake ref", fake_err.call_args.args[0]
                )

    def test_a_blank_remembered_ref_is_a_missing_one(self):
        for text in ("", "\n", "  \n\t"):
            with self.subTest(text=text):
                self.remember(text)
                self.assertIsNone(yo.remembered_flake_ref())
                with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
                    self.assertIn("v1.2.3#yolobox", yo.rebuild_hint())

    def test_a_remembered_ref_that_is_not_utf8_is_read_not_a_crash(self):
        os.makedirs(os.path.dirname(yo.flake_ref_file()), exist_ok=True)
        with open(yo.flake_ref_file(), "wb") as handle:
            handle.write(b"github:x/y/\xff\n")
        self.assertEqual(yo.remembered_flake_ref(), "github:x/y/\udcff")


class TestMainPreflight(unittest.TestCase):
    def test_version_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo.proc, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo.proc, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_works_in_a_fresh_process_with_no_machine_chosen(self):
        launcher = str(yo.REPO / "yo")
        for argv in (["--help"], ["help", "code"], ["zed", "--help"]):
            with self.subTest(argv=argv):
                done = subprocess.run(
                    [sys.executable, "-I", launcher, *argv],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertRegex(done.stdout, r'"Include ~/\S+/<machine>/ssh\.config"')

    def test_the_editor_help_names_the_hosts_instances_directory(self):
        for platform, directory in (
            ("darwin", "~/.lima/<machine>/ssh.config"),
            ("linux", "~/.local/state/yolobox/instances/<machine>/ssh.config"),
        ):
            with self.subTest(platform=platform):
                with mock.patch.object(yo.config, "HOST_PLATFORM", platform):
                    for command in ("code", "zed"):
                        self.assertIn(directory, self.help_text(["help", command]))

    def help_text(self, argv):
        """What `yo.main(argv)` prints, however it ends, with no limactl preflight."""
        buf = io.StringIO()
        with mock.patch.object(yo.proc, "require_tool", side_effect=AssertionError):
            with contextlib.redirect_stdout(buf):
                try:
                    self.assertEqual(yo.main(argv), 0)
                except SystemExit as caught:
                    self.assertEqual(caught.code, 0)
        return buf.getvalue().strip()

    def test_help_command_prints_the_top_level_help(self):
        self.assertEqual(self.help_text(["help"]), self.help_text(["--help"]))

    def test_help_command_prints_a_subcommands_help(self):
        self.assertEqual(
            self.help_text(["help", "bootstrap"]),
            self.help_text(["bootstrap", "--help"]),
        )

    def test_help_of_an_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["help", "bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_help_of_every_command_exits_zero(self):
        commands = [
            "up",
            "down",
            "status",
            "bootstrap",
            "gc",
            "disk-grow",
            "ssh",
            "enter",
            "cp",
            "code",
            "zed",
            "aws-check",
            "help",
        ]
        with mock.patch.object(yo.proc, "require_tool", side_effect=AssertionError):
            with contextlib.redirect_stdout(io.StringIO()):
                for name in commands:
                    with self.subTest(command=name):
                        self.assertEqual(yo.main(["help", name]), 0)

    def test_a_version_that_cannot_be_a_ref_is_a_usage_error_before_the_preflight(
        self,
    ):
        for version in ("1.7", "latest"):
            with self.subTest(version=version):
                with mock.patch.object(
                    yo.proc, "require_tool", side_effect=AssertionError("preflight ran")
                ):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            yo.main(["bootstrap", "--version", version])
                self.assertEqual(caught.exception.code, 2)

    def test_known_command_runs_the_preflight_before_the_command(self):
        def raise_reached(args):
            raise Reached()

        with mock.patch.object(
            yo.proc, "require_tool", side_effect=yo.YoError("limactl not found")
        ):
            with mock.patch.object(yo.status, "cmd_status", side_effect=raise_reached):
                with self.assertRaisesRegex(yo.YoError, "limactl not found"):
                    yo.main(["status"])

    def test_no_arguments_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_an_abbreviated_flag_is_a_usage_error(self):
        for argv in (["gc", "--y"], ["gc", "--de"], ["--vers"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        yo.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_unknown_command_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["bogus"])
        self.assertEqual(caught.exception.code, 2)

    def derived_names_seen_by_status(self, argv):
        seen = {}

        def record(args):
            seen.update(
                instance=yo.box.name,
                vm_host=yo.box.vm_host,
                include_line=yo.box.include_line,
                include_re=yo.box.include_re.pattern,
                editor_block=yo.box.editor_ssh_block,
            )

        try:
            with mock.patch.object(yo.proc, "require_tool"):
                with mock.patch.object(yo.status, "cmd_status", side_effect=record):
                    yo.main(argv)
        finally:
            yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)
        return seen

    def test_machine_flag_sets_every_derived_name(self):
        seen = self.derived_names_seen_by_status(["-m", "box2", "status"])
        self.assertEqual(seen["instance"], "box2")
        self.assertEqual(seen["vm_host"], "lima-box2")
        self.assertIn("box2", seen["include_line"])
        self.assertIn("box2", seen["include_re"])
        self.assertIn("lima-box2", seen["editor_block"])

    def test_machine_flag_is_accepted_after_the_command(self):
        seen = self.derived_names_seen_by_status(["status", "-m", "box2"])
        self.assertEqual(seen["instance"], "box2")
        self.assertEqual(seen["vm_host"], "lima-box2")

    def test_the_last_machine_flag_wins(self):
        seen = self.derived_names_seen_by_status(["-m", "box1", "status", "-m", "box2"])
        self.assertEqual(seen["instance"], "box2")

    def test_machine_flag_before_the_command_survives_the_subparser(self):
        seen = self.derived_names_seen_by_status(["--machine", "box2", "status"])
        self.assertEqual(seen["instance"], "box2")

    def test_machine_flag_before_ssh_leaves_the_guest_command_intact(self):
        args = yo.build_parser().parse_args(["ssh", "-m", "box2", "sudo", "ls", "-m"])
        self.assertEqual(args.machine, "box2")
        self.assertEqual(args.ssh_cmd, ["sudo", "ls", "-m"])

    def test_abbreviated_machine_flag_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.main(["--mach", "box2", "status"])
        self.assertEqual(caught.exception.code, 2)


class TestMainHealsBeforeSomeCommands(unittest.TestCase):
    # The commands that talk to the guest re-establish its forwards first, once,
    # after the machine is chosen and before the command runs. The ones that
    # stop, describe, clean or only forward a port never do, and up/bootstrap/
    # disk-grow heal inside their own flow.
    HEALING = [
        (["ssh"], (yo.session, "cmd_ssh")),
        (["enter"], (yo.session, "cmd_enter")),
        (["cp", ":a", "b"], (yo.cp, "cmd_cp")),
        (["code"], (yo.session, "cmd_code")),
        (["zed"], (yo.session, "cmd_zed")),
    ]
    NOT_HEALING = [
        (["down"], (yo.vm, "cmd_down")),
        (["status"], (yo.status, "cmd_status")),
        (["gc"], (yo.vm, "cmd_gc")),
        (["port", "8080"], (yo.session, "cmd_port")),
        (["aws-check"], (yo.session, "cmd_aws_check")),
        (["up"], (yo.vm, "vm_up")),
        (["bootstrap"], (yo.vm, "cmd_bootstrap")),
        (["disk-grow", "40"], (yo.vm, "cmd_disk_grow")),
    ]

    def run_main(self, argv, target, heal=None):
        calls = []
        with (
            mock.patch.object(yo.proc, "require_tool"),
            mock.patch.object(
                yo.forwards,
                "vm_services",
                side_effect=lambda: calls.append("heal") or heal and heal(),
            ),
            mock.patch.object(
                *target, side_effect=lambda *a, **k: calls.append("command")
            ),
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            try:
                yo.main(argv)
            finally:
                yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)
        return calls, fake_err

    def test_a_command_that_talks_to_the_guest_heals_once_before_it_runs(self):
        for argv, target in self.HEALING:
            with self.subTest(command=argv[0]):
                calls, _ = self.run_main(argv, target)
                self.assertEqual(calls, ["heal", "command"])

    def test_the_other_commands_never_heal_in_main(self):
        for argv, target in self.NOT_HEALING + [(["help"], (yo.ui, "out"))]:
            with self.subTest(command=argv[0]):
                calls, _ = self.run_main(argv, target)
                self.assertNotIn("heal", calls)

    def test_an_unreachable_box_stops_the_command(self):
        def lost():
            raise yo.sshx.unreachable(yo.OPERATOR)

        for argv, target in self.HEALING:
            with self.subTest(command=argv[0]):
                calls = []
                with self.assertRaisesRegex(yo.Unreachable, "is it up\\? yo status"):
                    calls, _ = self.run_main(argv, target, heal=lost)
                self.assertEqual(calls, [])

    def test_any_other_healing_failure_is_a_warning_and_the_command_runs(self):
        def broken():
            raise yo.YoError("aws-broker exploded")

        for argv, target in self.HEALING:
            with self.subTest(command=argv[0]):
                calls, fake_err = self.run_main(argv, target, heal=broken)
                self.assertEqual(calls, ["heal", "command"])
                warning = fake_err.call_args.args[0]
                self.assertIn("aws-broker exploded", warning)


class TestSshRemainderPassthrough(unittest.TestCase):
    # rebuild_hint() prints "yo ssh sudo YOLOBOX_USERNAME=$(id -un)
    # YOLOBOX_HOST_OS=<os> nixos-rebuild switch --impure --flake '<ref>#yolobox'"
    # as the documented remedy for a
    # box that has drifted from the host's yo. If argparse ever ate --impure or
    # --flake off that line, the tool's own printed advice would silently stop
    # working.
    def parse(self, argv):
        return yo.build_parser().parse_args(argv)

    def test_the_rebuild_hint_remedy_round_trips_intact(self):
        args = self.parse(
            [
                "ssh",
                "sudo",
                "YOLOBOX_USERNAME=x",
                "YOLOBOX_HOST_OS=darwin",
                "nixos-rebuild",
                "switch",
                "--impure",
                "--flake",
                ".#yolobox",
            ]
        )
        self.assertEqual(
            args.ssh_cmd,
            [
                "sudo",
                "YOLOBOX_USERNAME=x",
                "YOLOBOX_HOST_OS=darwin",
                "nixos-rebuild",
                "switch",
                "--impure",
                "--flake",
                ".#yolobox",
            ],
        )

    def test_a_plain_remote_command_round_trips_intact(self):
        args = self.parse(["ssh", "journalctl", "--user", "-u", "herdr", "-n", "50"])
        self.assertEqual(
            args.ssh_cmd, ["journalctl", "--user", "-u", "herdr", "-n", "50"]
        )

    def test_no_remote_command_is_an_empty_list(self):
        args = self.parse(["ssh"])
        self.assertEqual(args.ssh_cmd, [])


class TestUpRejectsExtraArguments(unittest.TestCase):
    def test_extra_positional_argument_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["up", "bogus"])
        self.assertEqual(caught.exception.code, 2)


class TestEnterProjectDistinguishesEmptyFromAbsent(unittest.TestCase):
    def test_no_project_argument_parses_to_none(self):
        args = yo.build_parser().parse_args(["enter"])
        self.assertIsNone(args.project)

    def test_empty_string_project_argument_parses_to_empty_string(self):
        args = yo.build_parser().parse_args(["enter", ""])
        self.assertEqual(args.project, "")

    def test_target_dir_picks_a_project_only_when_one_was_given(self):
        with mock.patch.object(
            yo.session, "pick_project", return_value="picked"
        ) as fake_pick:
            with mock.patch.object(yo.paths, "dir_for_cwd") as fake_cwd:
                self.assertEqual(yo.target_dir(""), "picked")
        fake_pick.assert_called_once_with("")
        fake_cwd.assert_not_called()

    def test_target_dir_falls_back_to_the_cwd_when_no_project_was_given(self):
        with mock.patch.object(yo.session, "pick_project") as fake_pick:
            with mock.patch.object(
                yo.paths, "dir_for_cwd", return_value="cwd"
            ) as fake_cwd:
                self.assertEqual(yo.target_dir(None), "cwd")
        fake_pick.assert_not_called()
        fake_cwd.assert_called_once_with()


class TestHelpExitsZero(unittest.TestCase):
    def test_top_level_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_gc_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                yo.build_parser().parse_args(["gc", "--help"])
        self.assertEqual(caught.exception.code, 0)


class TestBootstrapRebootDecision(FakeHome):
    def setUp(self):
        super().setUp()
        for module, name in (
            (yo.vm, "vm_up"),
            (yo.forwards, "vm_services"),
            (yo.forwards, "ensure_herdr_machine"),
            (yo.guest, "ensure_guest_known_hosts"),
        ):
            patcher = mock.patch.object(module, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        fstype_patcher = mock.patch.object(
            yo.vm, "luks_device_fstype", return_value="crypto_LUKS"
        )
        fstype_patcher.start()
        self.addCleanup(fstype_patcher.stop)

        ssh_run_patcher = mock.patch.object(
            yo.sshx,
            "ssh_run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        )
        ssh_run_patcher.start()
        self.addCleanup(ssh_run_patcher.stop)

        include_patcher = mock.patch.object(
            yo.sshconfig, "ssh_config_include_line", return_value=1
        )
        include_patcher.start()
        self.addCleanup(include_patcher.stop)

        out_patcher = mock.patch.object(yo.ui, "out")
        out_patcher.start()
        self.addCleanup(out_patcher.stop)

        err_patcher = mock.patch.object(yo.ui, "err")
        err_patcher.start()
        self.addCleanup(err_patcher.stop)

    def run_bootstrap_default(self):
        args = yo.build_parser().parse_args(["bootstrap"])
        with mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g1")):
            with mock.patch.object(yo.proc, "run"):
                yo.cmd_bootstrap(args)

    def run_bootstrap_with_real_generations(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with mock.patch.object(yo.proc, "run", side_effect=fake_run):
            yo.cmd_bootstrap(args)
        return calls

    def run_bootstrap(self, generations):
        with mock.patch.object(yo.vm, "guest_generations", side_effect=generations):
            return self.run_bootstrap_with_real_generations()

    def test_a_box_without_the_helper_still_reaches_its_reboot(self):
        # The plain nixos-lima image a first bootstrap starts from has no
        # yolobox-guest; the generations are read over plain ssh.
        reads = iter(["/store/g1\n/store/g0\n", "/store/g1\n/store/g1\n"])
        asked = []

        def fake_ssh_run(role, argv, **kwargs):
            asked.append(list(argv))
            if argv[0] == yo.HELPER:
                return mock.Mock(
                    returncode=127, stdout="", stderr="%s: not found" % yo.HELPER
                )
            if argv[0] == "sh":
                return mock.Mock(returncode=0, stdout=next(reads), stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        yo.ssh_run.side_effect = fake_ssh_run
        calls = self.run_bootstrap_with_real_generations()
        self.assertEqual(calls, [["limactl", "stop", "yolobox"]])
        self.assertFalse([argv for argv in asked if argv[0] == yo.HELPER])

    def test_no_restart_when_already_running_the_built_generation(self):
        calls = self.run_bootstrap([("g1", "g1")])
        self.assertNotIn(["limactl", "restart", "yolobox"], calls)

    def test_the_ref_it_built_from_is_remembered_for_the_rebuild_hint(self):
        self.run_bootstrap([("g1", "g1")])
        with open(yo.flake_ref_file()) as handle:
            self.assertEqual(handle.read().strip(), "github:x/y/z")

    def test_an_interrupt_while_remembering_the_ref_leaves_no_temp_file(self):
        with mock.patch.object(yo.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_bootstrap([("g1", "g1")])
        self.assertEqual(
            [name for name in os.listdir(yo.state_dir()) if name.startswith("tmp")], []
        )
        self.assertFalse(os.path.exists(yo.flake_ref_file()))

    def test_a_default_bootstrap_forgets_the_ref_of_an_earlier_fork_bootstrap(self):
        self.run_bootstrap([("g1", "g1")])
        self.assertTrue(os.path.exists(yo.flake_ref_file()))
        with mock.patch.object(yo.config, "YO_VERSION", "1.7.2"):
            self.run_bootstrap_default()
            self.assertFalse(os.path.exists(yo.flake_ref_file()))
            self.assertIn("v1.7.2#yolobox", yo.rebuild_hint())

    def test_stops_exactly_once_then_starts_again_when_the_second_read_agrees(self):
        calls = self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(calls, [["limactl", "stop", "yolobox"]])
        self.assertEqual(
            [call.kwargs for call in yo.vm_up.call_args_list],
            [
                {"run_services": False, "size": yo.NO_SIZE},
                {"run_services": False, "passphrase": yo.vm_up.return_value},
            ],
        )

    def test_the_size_asked_for_reaches_only_the_first_vm_up(self):
        args = yo.build_parser().parse_args(
            [
                "bootstrap",
                "--version",
                "github:x/y/z",
                "--memory",
                "24",
                "--disk",
                "250",
            ]
        )
        with (
            mock.patch.object(
                yo.vm, "guest_generations", side_effect=[("g1", "g0"), ("g1", "g1")]
            ),
            mock.patch.object(yo.proc, "run"),
        ):
            yo.cmd_bootstrap(args)
        first, second = yo.vm_up.call_args_list
        self.assertEqual(first.kwargs["size"], yo.Size(memory_gib=24, disk_gib=250))
        self.assertEqual(second.kwargs.get("size", yo.NO_SIZE), yo.NO_SIZE)

    def test_the_passphrase_is_derived_before_the_vm_stops(self):
        # A missing 1Password key must leave the running box alone.
        yo.vm_up.return_value = None
        order = []
        with (
            mock.patch.object(
                yo.agent,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"a" * 64,
            ),
            mock.patch.object(
                yo.LIMA, "stop", side_effect=lambda: order.append("stop")
            ),
        ):
            self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(order, ["passphrase", "stop"])

    def test_vm_up_brings_the_vm_up_without_services(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_up.assert_called_once_with(run_services=False, size=yo.NO_SIZE)

    def test_vm_services_runs_once_after_deciding_the_generation_already_matches(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_runs_once_after_a_reboot_settles_the_generation(self):
        self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_does_not_run_when_the_generation_never_settles(self):
        with mock.patch.object(yo.ui, "err"):
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        yo.vm_services.assert_not_called()

    def test_aborts_naming_boot_partition_when_generations_still_disagree(self):
        with mock.patch.object(yo.ui, "err") as fake_err:
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("df /boot", messages)

    def test_aborts_naming_yo_status_when_booted_is_unreadable(self):
        with mock.patch.object(yo.ui, "err") as fake_err:
            with self.assertRaises(yo.YoError) as caught:
                self.run_bootstrap([("g1", "")])
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("yo status", messages)

    def test_failed_stop_aborts_naming_the_recovery(self):
        def fake_run(argv, **kwargs):
            if argv[:2] == ["limactl", "stop"]:
                raise yo.YoError("limactl stop yolobox failed (exit 1)")
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with (
            mock.patch.object(yo.ui, "err") as fake_err,
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g0")),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_bootstrap(args)
            self.assertEqual(
                (caught.exception.message, caught.exception.code), (None, 1)
            )
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("limactl stop failed", messages)
        self.assertIn("yo bootstrap", messages)

    def test_herdr_is_told_whether_the_ssh_include_is_in_place(self):
        for included in (True, False):
            with self.subTest(included=included):
                with mock.patch.object(
                    yo.sshconfig, "ensure_ssh_config_include", return_value=included
                ):
                    self.run_bootstrap([("g1", "g1")])
                yo.ensure_herdr_machine.assert_called_with(included)

    def test_ssh_include_and_herdr_run_before_vm_services(self):
        # M1: an optional-service failure in vm_services must never skip
        # the ssh config include or herdr registration —
        # so vm_services has to be the very last thing cmd_bootstrap does.
        order = []

        def record(name):
            def side_effect(*args, **kwargs):
                order.append(name)

            return side_effect

        for module, name in (
            (yo.sshconfig, "ensure_ssh_config_include"),
            (yo.guest, "ensure_guest_known_hosts"),
            (yo.forwards, "ensure_herdr_machine"),
            (yo.forwards, "vm_services"),
        ):
            patcher = mock.patch.object(module, name, side_effect=record(name))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.run_bootstrap([("g1", "g1")])

        self.assertEqual(
            order,
            [
                "ensure_ssh_config_include",
                "ensure_guest_known_hosts",
                "ensure_herdr_machine",
                "vm_services",
            ],
        )


class TestBootstrapWarnsBeforeEncryptingInPlace(FakeHome):
    def run_bootstrap(self, fstype, df_line):
        rebuilds = self.rebuilds = []

        def fake_ssh_run(role, argv, **kwargs):
            if argv[:2] == ["lsblk", "-dno"]:
                return mock.Mock(returncode=0, stdout=fstype + "\n", stderr="")
            if argv == yo.guest.ROOT_USAGE_PROBE:
                if isinstance(df_line, mock.Mock):
                    return df_line
                return mock.Mock(returncode=0, stdout=df_output(df_line), stderr="")
            if "nixos-rebuild" in argv:
                rebuilds.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap", "--version", "github:x/y/z"])
        with (
            mock.patch.object(yo.vm, "vm_up"),
            mock.patch.object(yo.forwards, "vm_services"),
            mock.patch.object(yo.forwards, "ensure_herdr_machine"),
            mock.patch.object(yo.guest, "ensure_guest_known_hosts"),
            mock.patch.object(yo.sshconfig, "ensure_ssh_config_include"),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(yo.vm, "guest_generations", return_value=("g1", "g1")),
            mock.patch.object(yo.ui, "out"),
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            try:
                yo.cmd_bootstrap(args)
            finally:
                self.stderr = " ".join(call.args[0] for call in fake_err.call_args_list)
        return rebuilds

    def test_an_unencrypted_disk_gets_the_no_interruption_warning(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 500 500 50% /")
        self.assertEqual(len(rebuilds), 1)
        self.assertIn("encrypts /dev/vda2 in place", self.stderr)
        self.assertIn("must not be interrupted", self.stderr)

    def test_the_rebuild_names_the_username_host_os_and_hostname(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 500 500 50% /")
        self.assertEqual(
            rebuilds[0][:5],
            [
                "sudo",
                "YOLOBOX_USERNAME=%s" % yo.host_user(),
                "YOLOBOX_HOST_OS=darwin",
                "YOLOBOX_HOSTNAME=yolobox",
                "nixos-rebuild",
            ],
        )

    def test_an_encrypted_disk_gets_no_warning(self):
        rebuilds = self.run_bootstrap(
            "crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /"
        )
        self.assertEqual(len(rebuilds), 1)
        self.assertNotIn("in place", self.stderr)

    def test_a_disk_over_90_percent_full_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            self.run_bootstrap("ext4", "/dev/vda2 1000 950 50 95% /")
        self.assertIn("95% full", caught.exception.message)
        self.assertIn("yo gc --deep --yes", caught.exception.message)
        self.assertEqual(self.rebuilds, [])

    def test_exactly_90_percent_full_is_still_allowed(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 900 100 90% /")
        self.assertEqual(len(rebuilds), 1)

    def test_an_unreadable_df_is_an_error_not_a_pass(self):
        with self.assertRaisesRegex(
            yo.YoError, "cannot read the VM's root usage from df:"
        ):
            self.run_bootstrap("ext4", "garbage")

    def test_a_df_that_ssh_could_not_run_names_what_ssh_said(self):
        ssh_failed = mock.Mock(returncode=255, stdout="", stderr="ssh: no route")
        with self.assertRaisesRegex(yo.YoError, r"from df \(exit 255\): ssh: no route"):
            self.run_bootstrap("ext4", ssh_failed)
        self.assertEqual(self.rebuilds, [])

    def test_an_unreadable_device_type_is_an_error_not_a_skipped_check(self):
        with self.assertRaises(yo.YoError) as caught:
            self.run_bootstrap("", "/dev/vda2 1000 950 50 95% /\n")
        self.assertIn("cannot read the type of /dev/vda2", caught.exception.message)
        self.assertIn("yo status", caught.exception.message)
        self.assertEqual(self.rebuilds, [])


class TestGcSkipsCollectionOnHalfFailedSwitch(unittest.TestCase):
    # This reads nix/guest/yolobox-guest.sh off disk to keep Python and Nix
    # honest with each other. The guard this exercises moved from yo's
    # GC_MACHINE string into cmd_gc_machine there, so the test reads the
    # guard from that file rather than from a string literal in yo.
    def run_decision(self, booted, profile):
        guest_script = yo.REPO / "nix" / "guest" / "yolobox-guest.sh"
        text = guest_script.read_text()

        generations_fn = re.search(r"generations\(\) \{.*?\n\}\n", text, re.S)
        self.assertIsNotNone(generations_fn)

        decision = re.search(
            r"    local gen_out profile_line booted_line profile booted\n.*?\n    fi\n",
            text,
            re.S,
        )
        self.assertIsNotNone(decision)

        script = (
            "set -uo pipefail\n"
            "GC_FAILED=0\n"
            "readlink() {\n"
            '  case "$2" in\n'
            "    /run/current-system) printf '%s\\n' \"$BOOTED_VALUE\" ;;\n"
            "    /nix/var/nix/profiles/system) printf '%s\\n' \"$PROFILE_VALUE\" ;;\n"
            "  esac\n"
            "}\n"
            "step() { printf 'STEP:%s\\n' \"$1\"; }\n"
            "fail() { printf 'FAIL:%s\\n' \"$1\"; }\n"
            + generations_fn.group(0)
            + "run_decision() {\n"
            + decision.group(0)
            + "}\n"
            + "run_decision\n"
        )
        env = dict(os.environ, BOOTED_VALUE=booted, PROFILE_VALUE=profile)
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env
        )
        return result.stdout

    def test_runs_the_collection_when_booted_matches_the_profile(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/aaa")
        self.assertIn("STEP:nix-collect-garbage -d", stdout)
        self.assertIn("STEP:nix-store --optimise", stdout)

    def test_skips_the_collection_when_the_generations_disagree(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/bbb")
        self.assertIn(
            "FAIL:skipped nix-collect-garbage -d: booted and profile generations disagree",
            stdout,
        )
        self.assertNotIn("STEP:", stdout)


class TestDirForCwdLandsOnDeepestExistingAncestor(FakeHome):
    def test_lands_on_the_deepest_existing_ancestor(self):
        desired = self.home + "/a/b"
        landing = yo.AGENT_HOME + "/a"
        proc = mock.Mock(returncode=0, stdout=landing + "\n", stderr="")
        with mock.patch.object(yo.paths, "logical_cwd", return_value=desired):
            with mock.patch.object(
                yo.sshx, "ssh_run", return_value=proc
            ) as fake_ssh_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    result = yo.dir_for_cwd()
        self.assertEqual(result, landing)
        fake_err.assert_called_once_with(
            yo.MISSING_PATH_NOTE % (yo.AGENT_HOME + "/a/b", landing)
        )
        fake_ssh_run.assert_called_once_with(
            yo.AGENT, [yo.HELPER, "landing-dir", yo.AGENT_HOME + "/a/b"]
        )

    def test_a_landing_dir_outside_the_guest_home_is_refused_unprinted(self):
        reported = "/etc/\x1b]0;pwned\x07"
        proc = mock.Mock(returncode=0, stdout=reported + "\n", stderr="")
        with mock.patch.object(yo.paths, "logical_cwd", return_value=self.home + "/a"):
            with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    with self.assertRaisesRegex(
                        yo.YoError,
                        r'refusing to target "/etc/\\x1b\]0;pwned\\x07": no yo',
                    ):
                        yo.dir_for_cwd()
        fake_err.assert_not_called()

    def test_a_landing_dir_with_an_escape_is_reported_escaped(self):
        landing = yo.AGENT_HOME + "/a\x1b]0;pwned\x07"
        proc = mock.Mock(returncode=0, stdout=landing + "\n", stderr="")
        with mock.patch.object(
            yo.paths, "logical_cwd", return_value=self.home + "/a/b"
        ):
            with mock.patch.object(yo.sshx, "ssh_run", return_value=proc):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    yo.dir_for_cwd()
        (line,) = fake_err.call_args.args
        self.assertIn(r'"/home/agent/a\x1b]0;pwned\x07"', line)
        self.assertTrue(line.isprintable())

    def test_outside_home_lands_on_the_agent_home(self):
        with mock.patch.object(yo.paths, "logical_cwd", return_value="/etc"):
            with mock.patch.object(yo.sshx, "ssh_run") as fake_ssh_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    result = yo.dir_for_cwd()
        fake_ssh_run.assert_not_called()
        self.assertEqual(result, yo.AGENT_HOME)
        fake_err.assert_called_once_with(yo.MISSING_PATH_NOTE % ("/etc", yo.AGENT_HOME))


class TestTerminalSanitising(unittest.TestCase):
    def printed(self, func, text):
        stream = io.StringIO()
        target = "sys.stdout" if func is yo.out else "sys.stderr"
        with mock.patch(target, stream):
            func(text)
        return stream.getvalue()

    def test_escape_and_c1_controls_are_replaced_on_both_streams(self):
        for func in (yo.out, yo.err):
            with self.subTest(func=func.__name__):
                self.assertEqual(
                    self.printed(func, "a\x1b]0;x\x07b\x9bc\x7fd\x00e\rf"),
                    "a?]0;x?b?c?d?e?f\n",
                )

    def test_tab_newline_and_printable_unicode_survive(self):
        for func in (yo.out, yo.err):
            with self.subTest(func=func.__name__):
                self.assertEqual(
                    self.printed(func, "a\tb\nc \u00a0é—"), "a\tb\nc \u00a0é—\n"
                )


class TestPickProjectSanitisesWhatFzfSees(unittest.TestCase):
    def test_a_guest_controlled_name_reaches_fzf_without_control_bytes(self):
        picker = mock.Mock(returncode=0, stdout=b"ok")
        with (
            mock.patch.object(yo.proc, "require_tool"),
            mock.patch.object(
                yo.guest,
                "agent_paths",
                return_value=["/home/agent/ok", "/home/agent/a\x1b]52;c;eA==\x07b"],
            ),
            mock.patch.object(yo.subprocess, "run", return_value=picker) as run,
        ):
            yo.pick_project("")
        self.assertEqual(run.call_args.kwargs["input"], b"a?]52;c;eA==?b\0ok")


class TestStreamedOutputIsFiltered(unittest.TestCase):
    # yo gc streams the guest tiers live; a Makefile recipe they echo must not
    # reach the terminal as an escape sequence.
    def test_a_streamed_command_is_filtered_and_keeps_its_exit_code(self):
        script = (
            "import sys; sys.stdout.write('o\\x1b]52;c;eA==\\x07\\n');"
            "sys.stderr.write('e\\x1b[2J\\n'); sys.exit(3)"
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            proc = yo.run([sys.executable, "-c", script], filtered=True)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(stdout.getvalue(), "o?]52;c;eA==?\n")
        self.assertEqual(stderr.getvalue(), "e?[2J\n")


class TestEditorUris(unittest.TestCase):
    def launched(self, command, tool):
        argv = []
        with (
            mock.patch.object(yo.shutil, "which", return_value=tool),
            mock.patch.object(yo.os, "access", return_value=True),
            mock.patch.object(yo.sshconfig, "ensure_ssh_config_include"),
            mock.patch.object(yo.sshx, "require_agent_account"),
            mock.patch.object(
                yo.session, "target_dir", return_value=yo.AGENT_HOME + "/a b/c#d?e%f"
            ),
            mock.patch.object(yo.proc, "exec_process", side_effect=argv.extend),
        ):
            args = yo.build_parser().parse_args([command])
            args.func(args)
        return argv[-1]

    def test_zed_uri_percent_encodes_the_project_path(self):
        self.assertEqual(
            self.launched("zed", "/bin/zed"),
            "ssh://agent@lima-yolobox/home/agent/a%20b/c%23d%3Fe%25f",
        )

    def test_code_uri_percent_encodes_the_project_path(self):
        self.assertEqual(
            self.launched("code", "/bin/code"),
            "vscode-remote://ssh-remote+agent@lima-yolobox/home/agent/a%20b/c%23d%3Fe%25f",
        )

    def test_an_ordinary_path_is_unchanged(self):
        path = "/home/agent/src/myproject"
        self.assertEqual(yo.editor_uri_path(path), path)


class TestProbe(unittest.TestCase):
    # ssh exits 255 when it cannot connect, so that code says nothing about the
    # command it was to run. The four callers that read an exit code as a fact
    # about the box (a missing unit, a missing helper, the box's version) must
    # see Unreachable first.
    def lost_ssh(self):
        return mock.patch.object(
            yo.sshx,
            "ssh_run",
            return_value=mock.Mock(returncode=yo.SSH_UNREACHABLE, stdout="", stderr=""),
        )

    def test_a_command_that_ran_is_returned_whatever_it_exited_with(self):
        for code in (0, 1, 64, 127):
            with self.subTest(code=code):
                ran = mock.Mock(returncode=code)
                with mock.patch.object(yo.sshx, "ssh_run", return_value=ran):
                    self.assertIs(yo.sshx.probe(yo.OPERATOR, ["true"]), ran)

    def test_exit_255_is_unreachable_with_the_is_it_up_hint(self):
        for role, who in ((yo.OPERATOR, "operator"), (yo.AGENT, "agent")):
            with self.subTest(role=who), self.lost_ssh():
                with self.assertRaisesRegex(
                    yo.Unreachable,
                    "cannot reach yolobox as %s — is it up\\? yo status" % who,
                ):
                    yo.sshx.probe(role, ["true"])

    def test_probe_passes_ssh_run_options_through(self):
        with mock.patch.object(
            yo.sshx, "ssh_run", return_value=mock.Mock(returncode=0)
        ) as fake:
            yo.sshx.probe(yo.AGENT, ["x"], binary=True, mux=True)
        fake.assert_called_once_with(yo.AGENT, ["x"], binary=True, mux=True)

    def test_a_missing_unit_is_false_but_a_lost_box_is_unreachable(self):
        with mock.patch.object(
            yo.sshx, "ssh_run", return_value=mock.Mock(returncode=1)
        ):
            self.assertFalse(yo.vm_units_present())
        with self.lost_ssh(), self.assertRaises(yo.Unreachable):
            yo.vm_units_present()

    def test_a_lost_box_is_not_a_helper_skew(self):
        for stream in (False, True):
            with self.subTest(stream=stream), self.lost_ssh():
                with self.assertRaises(yo.Unreachable):
                    yo.guest_helper(yo.AGENT, "projects", stream=stream)

    def test_the_skew_message_does_not_guess_a_version_when_the_box_is_lost(self):
        def run(role, argv, **kwargs):
            if argv[:1] == ["cat"]:
                return mock.Mock(returncode=yo.SSH_UNREACHABLE, stdout="", stderr="")
            return mock.Mock(returncode=0)

        with mock.patch.object(yo.sshx, "ssh_run", side_effect=run):
            with self.assertRaises(yo.Unreachable):
                yo.helper_skew("the box has no yolobox-guest")


class TestGuestOutNamesALostBox(unittest.TestCase):
    # guest_out reads a command's output as a fact about the box. A lost box
    # (ssh exit 255) must say so, not read as the empty output of a command
    # that found nothing.
    def answering(self, code, stdout="", stderr=""):
        return mock.patch.object(
            yo.sshx,
            "ssh_run",
            return_value=mock.Mock(returncode=code, stdout=stdout, stderr=stderr),
        )

    def test_a_command_that_succeeded_reads_as_its_output(self):
        with self.answering(0, "crypto_LUKS\n"):
            self.assertEqual(yo.sshx.guest_out(["lsblk"]), "crypto_LUKS\n")
            self.assertEqual(yo.vm.luks_device_fstype(), "crypto_LUKS")

    def test_a_command_that_failed_there_is_an_error_with_its_reason(self):
        # Its output is not an answer: an empty one would read as "nothing found".
        with self.answering(1, "", "lsblk: /dev/vda2: not a block device\n"):
            with self.assertRaises(yo.YoError) as caught:
                yo.sshx.guest_out(["lsblk", "/dev/vda2"])
        self.assertNotIsInstance(caught.exception, yo.Unreachable)
        self.assertEqual(
            caught.exception.message,
            "lsblk /dev/vda2 failed on the box (exit 1): "
            "lsblk: /dev/vda2: not a block device",
        )
        with self.answering(7):
            with self.assertRaisesRegex(yo.YoError, r"failed on the box \(exit 7\)$"):
                yo.vm.luks_device_fstype()

    def test_a_lost_box_is_unreachable_at_every_read_of_the_guest_disk(self):
        reads = (
            ("luks_device_fstype", yo.vm.luks_device_fstype),
            ("guest_generations", yo.vm.guest_generations),
            ("require_growable_guest", yo.vm.require_growable_guest),
            (
                "require_partition_grown",
                lambda: yo.vm.require_partition_grown(100),
            ),
        )
        for name, read in reads:
            with self.subTest(read=name), self.answering(yo.SSH_UNREACHABLE):
                with self.assertRaisesRegex(
                    yo.Unreachable, "cannot reach yolobox as operator — is it up\\?"
                ):
                    read()

    def test_status_says_why_the_type_could_not_be_read(self):
        with self.answering(1, "", "lsblk: /dev/vda2: not a block device\n"):
            lines = yo.status.disk_lines(vm_reachable=True)
        unreadable = [line for line in lines if "cannot read the type" in line.text]
        self.assertEqual(len(unreadable), 1)
        self.assertIn("not a block device", unreadable[0].text)
        self.assertFalse(unreadable[0].warning)

    def test_status_says_the_box_was_lost_instead_of_an_unreadable_type(self):
        with self.answering(yo.SSH_UNREACHABLE):
            lines = yo.status.disk_lines(vm_reachable=True)
        lost = [line for line in lines if line.text.startswith("disk: cannot reach")]
        self.assertEqual([line.warning for line in lost], [True])
        self.assertFalse(
            [line for line in lines if "cannot read the type" in line.text]
        )


class TestGuestHelperSkew(unittest.TestCase):
    # guest_helper reinterprets a subcommand's own exit code only when the
    # stderr marker backs it up, so a child process (podman, npm) that
    # happens to exit 127 or 64 on its own must pass through untouched — the
    # last two cases here are what stops that misread.
    def fake_ssh_run(self, main_proc, guest_version="1.1.0"):
        version_proc = mock.Mock(returncode=0, stdout=guest_version + "\n", stderr="")

        def run(role, argv, **kwargs):
            if argv[:1] == ["cat"]:
                return version_proc
            return main_proc

        return run

    def test_helper_absent_names_yo_bootstrap(self):
        main_proc = mock.Mock(
            returncode=127,
            stderr="bash: line 1: yolobox-guest: command not found\n",
            stdout="",
        )
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo.sshx, "ssh_run", side_effect=self.fake_ssh_run(main_proc)
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.AGENT, "projects")
        self.assertIn("yo bootstrap", caught.exception.message)

    def test_subcommand_newer_than_the_box_names_the_subcommand(self):
        main_proc = mock.Mock(
            returncode=64,
            stderr="yolobox-guest: unknown subcommand 'projects'\n",
            stdout="",
        )
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo.sshx, "ssh_run", side_effect=self.fake_ssh_run(main_proc)
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.AGENT, "projects")
        self.assertIn("projects", caught.exception.message)

    def test_a_childs_own_exit_64_with_no_marker_is_not_reinterpreted(self):
        main_proc = mock.Mock(returncode=64, stderr="", stdout="")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=main_proc):
            result = yo.guest_helper(yo.AGENT, "projects")
        self.assertIs(result, main_proc)

    def test_a_childs_own_exit_127_with_unrelated_stderr_is_not_reinterpreted(self):
        main_proc = mock.Mock(returncode=127, stderr="podman: exec failed\n", stdout="")
        with mock.patch.object(yo.sshx, "ssh_run", return_value=main_proc):
            result = yo.guest_helper(yo.AGENT, "projects")
        self.assertIs(result, main_proc)

    # A streamed run (yo gc) passes the tiers' output straight through, so
    # there is no captured stderr to match the marker against. The two skews
    # are told apart by asking the box whether the helper exists at all.
    def streamed_ssh_run(self, main_proc, helper_present):
        def run(role, argv, **kwargs):
            if argv[:1] == ["cat"]:
                return mock.Mock(returncode=0, stdout="1.1.0\n", stderr="")
            if argv[:2] == ["command", "-v"]:
                return mock.Mock(
                    returncode=0 if helper_present else 1, stdout="", stderr=""
                )
            return main_proc

        return run

    def test_streamed_absent_helper_names_yo_bootstrap(self):
        main_proc = mock.Mock(returncode=127, stderr=None, stdout=None)
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo.sshx,
                "ssh_run",
                side_effect=self.streamed_ssh_run(main_proc, helper_present=False),
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIn("yo bootstrap", caught.exception.message)

    def test_streamed_unknown_subcommand_names_the_subcommand(self):
        main_proc = mock.Mock(returncode=64, stderr=None, stdout=None)
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo.sshx,
                "ssh_run",
                side_effect=self.streamed_ssh_run(main_proc, helper_present=True),
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIn("gc-machine", caught.exception.message)

    def test_streamed_tier_failure_is_not_reinterpreted_as_skew(self):
        main_proc = mock.Mock(returncode=1, stderr=None, stdout=None)
        with mock.patch.object(yo.sshx, "ssh_run", return_value=main_proc):
            result = yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIs(result, main_proc)


class TestGcArgv(unittest.TestCase):
    # ext4's root reserve is the operator's alone (see docs/disk.md#recognising-a-full-disk), so on a
    # genuinely full disk the machine tier must run, and complete, before the
    # agent-tier steps that depend on the space it frees. This is the guard
    # against that ordering being silently swapped.
    def test_deep_yes_runs_machine_then_user_in_that_order(self):
        calls = []

        def fake_guest_helper(role, sub, args=(), **kwargs):
            calls.append((role, sub, list(args)))
            return mock.Mock(returncode=0)

        args = yo.build_parser().parse_args(["gc", "--deep", "--yes"])
        with mock.patch.object(yo.guest, "guest_helper", side_effect=fake_guest_helper):
            with mock.patch.object(yo.sshx, "require_agent_account"):
                yo.cmd_gc(args)

        self.assertEqual(
            calls,
            [
                (yo.OPERATOR, "gc-machine", ["--apply"]),
                (yo.AGENT, "gc-user", ["--apply", "--deep"]),
            ],
        )


class TestHelperFailureMessage(unittest.TestCase):
    def message(self, code, stderr=""):
        return yo.helper_failure_message(
            yo.HelperRun(code, stderr, migrated=False, streamed=False)
        )

    def test_an_unexpected_failure_shows_the_command_the_helper_named(self):
        stderr = "yolobox-unlock: unexpected failure (exit 4) at line 51: sed -n p x"
        self.assertEqual(
            self.message(yo.HELPER_EXIT_INTERNAL, stderr),
            "the initrd helper failed unexpectedly; the line below names the "
            "command that failed:\n" + stderr,
        )

    def test_every_documented_code_has_its_own_message(self):
        documented = (
            yo.HELPER_EXIT_WRONG_KEY,
            yo.HELPER_EXIT_MIGRATION_FAILED,
            yo.HELPER_EXIT_WATCH_LOST,
            yo.HELPER_EXIT_NO_PASSPHRASE,
            yo.HELPER_EXIT_ROOT_MOUNT_FAILED,
            yo.HELPER_EXIT_INTERNAL,
        )
        messages = {code: self.message(code, "E") for code in documented}
        self.assertEqual(len(set(messages.values())), len(documented))
        for code, message in messages.items():
            self.assertNotIn("failed with exit", message, code)

    def test_an_undocumented_code_is_named_with_its_number(self):
        self.assertEqual(
            self.message(9, "boom"), "the initrd helper failed with exit 9: boom"
        )


class TestCmdSshArgv(FakeHome):
    def ssh(self, stdin_tty, stdout_tty, cmd=()):
        captured = {}
        args = yo.build_parser().parse_args(["ssh", *cmd])
        fake_stdin, fake_stdout = mock.Mock(), mock.Mock()
        fake_stdin.isatty.return_value = stdin_tty
        fake_stdout.isatty.return_value = stdout_tty
        with (
            mock.patch.object(yo.sys, "stdin", fake_stdin),
            mock.patch.object(yo.sys, "stdout", fake_stdout),
            mock.patch.object(
                yo.proc,
                "exec_process",
                side_effect=lambda argv: captured.update(argv=argv),
            ),
        ):
            yo.cmd_ssh(args)
        return captured["argv"]

    def test_a_pty_is_requested_only_when_stdin_and_stdout_are_terminals(self):
        for stdin_tty, stdout_tty, wanted in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                argv = self.ssh(stdin_tty, stdout_tty)
                self.assertEqual("-t" in argv, wanted)

    def test_the_command_follows_the_host_either_way(self):
        for tty in (True, False):
            with self.subTest(tty=tty):
                argv = self.ssh(tty, tty, ["sudo", "ls"])
                self.assertEqual(argv[-3:], ["lima-yolobox", "sudo", "ls"])


class TestEntryExitCodes(unittest.TestCase):
    def test_a_closed_pipe_exits_141_not_success(self):
        # `yo status | head -1`: the shell reports a process killed by SIGPIPE as 141.
        with (
            mock.patch.object(yo.cli, "main", side_effect=BrokenPipeError),
            mock.patch.object(yo.os, "_exit") as fake_exit,
        ):
            yo.entry(["status"])
        fake_exit.assert_called_once_with(141)


class TestCmdEnterArgv(FakeHome):
    def enter(self, stdin_tty=True, stdout_tty=True):
        fake_stdin, fake_stdout = mock.Mock(), mock.Mock()
        fake_stdin.isatty.return_value = stdin_tty
        fake_stdout.isatty.return_value = stdout_tty
        with (
            mock.patch.object(yo.sys, "stdin", fake_stdin),
            mock.patch.object(yo.sys, "stdout", fake_stdout),
            mock.patch.object(yo.sshx, "require_agent_account") as self.agent_probe,
            mock.patch.object(yo.session, "target_dir", return_value=yo.AGENT_HOME),
            mock.patch.object(yo.proc, "exec_process") as self.exec,
        ):
            yo.cmd_enter(yo.build_parser().parse_args(["enter"]))
        return list(self.exec.call_args[0][0])

    def test_a_pty_is_requested_on_a_terminal(self):
        with env_without("HERDR_ENV"):
            self.assertIn("-t", self.enter())

    def test_a_shell_is_refused_unless_stdin_and_stdout_are_terminals(self):
        for stdin_tty, stdout_tty in ((True, False), (False, True), (False, False)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                with env_without("HERDR_ENV"):
                    with self.assertRaisesRegex(yo.YoError, "terminal"):
                        self.enter(stdin_tty, stdout_tty)
                self.exec.assert_not_called()
                self.agent_probe.assert_not_called()

    def test_argv_carries_no_forward_agent(self):
        with env_without("HERDR_ENV"):
            argv = self.enter()
        self.assertNotIn("ForwardAgent", " ".join(argv))

    def test_herdr_env_prints_the_invisibility_notice(self):
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with mock.patch.object(yo.ui, "err") as fake_err:
                self.enter()
        fake_err.assert_called_once()
        message = fake_err.call_args[0][0]
        self.assertIn("yolobox herdr machine", message)
        self.assertIn("invisible to herdr", message)

    def test_no_herdr_env_prints_nothing(self):
        with env_without("HERDR_ENV"):
            with mock.patch.object(yo.ui, "err") as fake_err:
                self.enter()
        fake_err.assert_not_called()


class TestEnsureHerdrMachine(unittest.TestCase):
    def test_no_herdr_on_path_skips_with_a_note(self):
        with mock.patch.object(yo.shutil, "which", return_value=None):
            with mock.patch.object(yo.proc, "run") as fake_run:
                with mock.patch.object(yo.ui, "err") as fake_err:
                    yo.ensure_herdr_machine(True)
        fake_run.assert_not_called()
        fake_err.assert_called_once()

    def test_existing_machine_is_left_alone(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(
                    returncode=0, stdout='[{"target": "yolobox"}]', stderr=""
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run) as tracked:
                yo.ensure_herdr_machine(True)
        calls = [call.args[0] for call in tracked.call_args_list]
        self.assertTrue(all(argv[2:3] != ["add"] for argv in calls))

    def test_missing_machine_is_added(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                yo.ensure_herdr_machine(True)
        self.assertIn(
            [
                "/opt/homebrew/bin/herdr",
                "machine",
                "add",
                "yolobox",
                "--label",
                "yolobox",
            ],
            calls,
        )

    def test_a_declined_ssh_include_skips_the_add_with_one_note_naming_the_line(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            raise AssertionError("herdr cannot resolve the host: %r" % (argv,))

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                with mock.patch.object(yo.ui, "err") as fake_err:
                    yo.ensure_herdr_machine(False)
        notes = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("~/.ssh/config does not include", notes)
        self.assertIn(yo.box.include_line, notes)

    def test_add_failure_raises_with_herdrs_stderr(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo.proc, "run", side_effect=fake_run):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_herdr_machine(True)
        self.assertIn("boom", caught.exception.message)


class TestVmServicesSkipsOnAnOldBox(unittest.TestCase):
    def run_vm_services(self, agent_ok, operator_ok, units_ok):
        def fake_ssh_run(role, argv, **kwargs):
            if argv == ["true"]:
                ok = agent_ok if role is yo.AGENT else operator_ok
                return mock.Mock(returncode=0 if ok else 1, stdout="", stderr="")
            if argv[:2] == ["systemctl", "cat"]:
                return mock.Mock(returncode=0 if units_ok else 1, stdout="", stderr="")
            raise AssertionError("unexpected ssh_run call: %r" % (argv,))

        with mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run):
            with (
                mock.patch.object(
                    yo.aws, "ensure_aws_broker", return_value=True
                ) as fake_aws,
                mock.patch.object(
                    yo.forwards, "ensure_forwards", return_value=[]
                ) as fake_forward,
                mock.patch.object(yo.ui, "err") as fake_err,
            ):
                yo.vm_services()
        return fake_aws, fake_forward, fake_err

    def test_operator_unreachable_raises_unreachable_and_heals_nothing(self):
        with (
            mock.patch.object(yo.aws, "ensure_aws_broker") as fake_aws,
            mock.patch.object(yo.forwards, "ensure_forwards") as fake_forward,
            mock.patch.object(
                yo.sshx, "account_status", return_value=("unreachable", "")
            ),
        ):
            with self.assertRaisesRegex(yo.Unreachable, "is it up\\? yo status"):
                yo.vm_services()
        fake_aws.assert_not_called()
        fake_forward.assert_not_called()

    def test_missing_units_skips_naming_bootstrap(self):
        fake_aws, fake_forward, fake_err = self.run_vm_services(
            agent_ok=False, operator_ok=True, units_ok=False
        )
        fake_aws.assert_not_called()
        fake_forward.assert_not_called()
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("yo bootstrap", messages)

    def test_units_present_runs_both_healers(self):
        fake_aws, fake_forward, fake_err = self.run_vm_services(
            agent_ok=True, operator_ok=True, units_ok=True
        )
        fake_aws.assert_called_once_with()
        fake_forward.assert_called_once()


class TestVmServicesRunsHealersIndependently(unittest.TestCase):
    def setUp(self):
        status_patcher = mock.patch.object(
            yo.sshx, "account_status", return_value=("agent", "")
        )
        status_patcher.start()
        self.addCleanup(status_patcher.stop)

        units_patcher = mock.patch.object(
            yo.guest, "vm_units_present", return_value=True
        )
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_a_broken_aws_broker_does_not_stop_the_forward_healing(self):
        with (
            mock.patch.object(
                yo.aws, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
            ),
            mock.patch.object(
                yo.forwards, "ensure_forwards", return_value=[]
            ) as fake_forward,
        ):
            with self.assertRaisesRegex(
                yo.YoError, "broker exploded \u2014 recreate the master: limactl"
            ):
                yo.vm_services()
        fake_forward.assert_called_once_with(False)

    def test_a_broker_that_cannot_be_disabled_is_reported_apart_without_the_master_remedy(
        self,
    ):
        with (
            mock.patch.object(
                yo.aws,
                "ensure_aws_broker",
                side_effect=yo.aws.BrokerDisableError("rm: Permission denied"),
            ),
            mock.patch.object(
                yo.forwards, "ensure_forwards", return_value=["1password forward dead"]
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_services()
        first, second = caught.exception.message.split("\n")
        self.assertRegex(first, "^1password forward dead .* recreate the master: ")
        self.assertEqual(second, "rm: Permission denied")

    def test_a_broker_that_cannot_be_disabled_alone_has_no_master_remedy(self):
        with (
            mock.patch.object(
                yo.aws,
                "ensure_aws_broker",
                side_effect=yo.aws.BrokerDisableError("rm: Permission denied"),
            ),
            mock.patch.object(yo.forwards, "ensure_forwards", return_value=[]),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_services()
        self.assertEqual(caught.exception.message, "rm: Permission denied")

    def test_errors_from_both_phases_are_collected_and_raised_together(self):
        with (
            mock.patch.object(
                yo.aws, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
            ),
            mock.patch.object(
                yo.forwards,
                "ensure_forwards",
                return_value=["1password forward dead"],
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_services()
        self.assertIn("broker exploded", caught.exception.message)
        self.assertIn("1password forward dead", caught.exception.message)

    def test_no_errors_from_either_phase_does_not_raise(self):
        with (
            mock.patch.object(yo.aws, "ensure_aws_broker", return_value=True),
            mock.patch.object(yo.forwards, "ensure_forwards", return_value=[]),
        ):
            yo.vm_services()


class TestHealReverseForward(FakeHome):
    def test_alive_probe_never_touches_the_forward(self):
        forward = yo.ReverseForward(
            "x", "/guest", "/host", ("probe",), lambda proc: True
        )
        with (
            mock.patch.object(
                yo.sshx, "ssh_run", return_value=mock.Mock(returncode=0)
            ) as fake_ssh,
            mock.patch.object(yo.proc, "run") as fake_run,
        ):
            self.assertTrue(yo.heal_reverse_forward(forward))
        fake_ssh.assert_called_once_with(yo.AGENT, ["probe"])
        fake_run.assert_not_called()

    def test_dead_probe_reissues_cancel_then_rm_then_forward(self):
        forward = yo.ReverseForward(
            "x", "/guest", "/host", ("probe",), lambda proc: proc.returncode == 0
        )
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(("run", list(argv), kwargs.get("check")))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            if argv == ["probe"]:
                calls.append(("probe", role))
                probe_count = len([c for c in calls if c[0] == "probe"])
                return mock.Mock(returncode=0 if probe_count >= 2 else 1)
            calls.append(("ssh_run", role, list(argv), kwargs.get("check")))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
        ):
            result = yo.heal_reverse_forward(forward)

        self.assertTrue(result)
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["probe", "run", "ssh_run", "run", "probe"])
        self.assertEqual(calls[1][1][-5:-2], ["-O", "cancel", "-R"])
        self.assertFalse(calls[1][2])
        self.assertEqual(calls[2][2], ["rm", "-f", "/guest"])
        self.assertEqual(calls[3][1][-5:-2], ["-O", "forward", "-R"])
        self.assertTrue(calls[3][2])


class TestEnsureOpForward(unittest.TestCase):
    def test_missing_mac_socket_skips_1password_but_still_heals_aws(self):
        with (
            mock.patch.object(yo.os.path, "exists", return_value=False),
            mock.patch.object(
                yo.forwards, "heal_reverse_forward", return_value=True
            ) as fake_heal,
            mock.patch.object(yo.ui, "err") as fake_err,
        ):
            errors = yo.ensure_forwards(aws_enabled=True)
        self.assertEqual(errors, [])
        fake_heal.assert_called_once()
        fake_err.assert_called_once()

    def test_aws_disabled_never_attempts_its_forward(self):
        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(
                yo.forwards, "heal_reverse_forward", return_value=True
            ) as fake_heal,
        ):
            yo.ensure_forwards(aws_enabled=False)
        self.assertEqual(fake_heal.call_count, 1)
        self.assertEqual(fake_heal.call_args[0][0].name, "1password")

    def test_both_forwards_failing_are_both_reported(self):
        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(yo.forwards, "heal_reverse_forward", return_value=False),
        ):
            errors = yo.ensure_forwards(aws_enabled=True)
        self.assertEqual(len(errors), 2)

    def test_1password_forward_raising_still_heals_the_aws_forward(self):
        def fake_heal(forward):
            if forward.name == "1password":
                raise yo.YoError("1password forward exploded")
            return False

        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(
                yo.forwards, "heal_reverse_forward", side_effect=fake_heal
            ) as fake_heal_mock,
        ):
            errors = yo.ensure_forwards(aws_enabled=True)
        self.assertEqual(fake_heal_mock.call_count, 2)
        self.assertTrue(any("1password forward exploded" in e for e in errors))
        self.assertTrue(any("aws-broker" in e for e in errors))

    def test_aws_forward_raising_does_not_swallow_the_1password_result(self):
        def fake_heal(forward):
            if forward.name == "aws-broker":
                raise yo.YoError("aws-broker forward exploded")
            return True

        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(
                yo.forwards, "heal_reverse_forward", side_effect=fake_heal
            ),
        ):
            errors = yo.ensure_forwards(aws_enabled=True)
        self.assertEqual(
            errors,
            [
                "the aws-broker forward into the VM failed while healing it: aws-broker forward exploded"
            ],
        )


class TestCmdAwsCheck(unittest.TestCase):
    def setUp(self):
        units_patcher = mock.patch.object(
            yo.guest, "vm_units_present", return_value=True
        )
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_units_missing_raises_naming_bootstrap_before_anything_else(self):
        with (
            mock.patch.object(yo.guest, "vm_units_present", return_value=False),
            mock.patch.object(yo.aws, "ensure_aws_broker") as fake_aws,
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertEqual(caught.exception.message, yo.units_missing_note())
        fake_aws.assert_not_called()

    def test_disabled_raises_naming_why(self):
        with mock.patch.object(yo.aws, "ensure_aws_broker", return_value=False):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertIn("disabled", caught.exception.message)

    def test_forward_heal_runs_before_the_guest_check(self):
        calls = []
        with (
            mock.patch.object(yo.aws, "ensure_aws_broker", return_value=True),
            mock.patch.object(
                yo.forwards,
                "heal_reverse_forward",
                side_effect=lambda fw: calls.append("heal") or True,
            ),
            mock.patch.object(
                yo.guest,
                "guest_helper",
                side_effect=lambda *a, **k: (
                    calls.append("guest") or mock.Mock(returncode=0)
                ),
            ),
        ):
            yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertEqual(calls, ["heal", "guest"])

    def test_dead_forward_raises_before_the_guest_check(self):
        with (
            mock.patch.object(yo.aws, "ensure_aws_broker", return_value=True),
            mock.patch.object(yo.forwards, "heal_reverse_forward", return_value=False),
            mock.patch.object(yo.guest, "guest_helper") as fake_guest,
        ):
            with self.assertRaisesRegex(
                yo.YoError, "the aws-broker forward into the VM is still"
            ):
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        fake_guest.assert_not_called()


HEALTHY_TAILSCALE = json.dumps({"BackendState": "Running"})
DF_HEALTHY = "/dev/vda2 59G 10G 46G 18% /"


class StatusRunner:
    """`yo status` run against a scripted guest that, by default, is healthy.

    A test names only what it breaks. `self.raised` holds the YoError that
    cmd_status ended with, None when it returned.
    """

    def run_status(
        self,
        *,
        units_present=True,
        gaps=(),
        host_gaps=(),
        aws_which=None,
        aws_allowlist_side_effect=lambda: [],
        net_check=None,
        fstype="crypto_LUKS\n",
        egress_probe="LoadState=loaded\nActiveState=active\n",
        table=None,
        ssh_answer=lambda role, argv: None,
        broker=None,
        keepalive_pid=777,
    ):
        outs = []
        # net_check is either the net-check subprocess result or the YoError
        # guest_helper raises on a box older than the subcommand.
        if net_check is None:
            net_check = mock.Mock(
                returncode=0, stdout="egress=ok\ngateway=ok\n", stderr=""
            )
        helper = (
            mock.Mock(side_effect=net_check)
            if isinstance(net_check, Exception)
            else mock.Mock(return_value=net_check)
        )
        if table is None:
            table = mock.Mock(
                returncode=0, stdout=(FIXTURES / "egress.nft").read_text(), stderr=""
            )

        def fake_out(line=""):
            outs.append(line)

        def fake_ssh_run(role, argv, **kwargs):
            answer = ssh_answer(role, argv)
            if answer is not None:
                return answer
            stdout = ""
            if argv[:2] == ["lsblk", "-dno"]:
                stdout = fstype
            elif argv == yo.guest.ROOT_USAGE_PROBE:
                stdout = df_output(human_line=DF_HEALTHY)
            elif argv == yo.EGRESS_PROBE:
                stdout = egress_probe
            elif argv == yo.EGRESS_TABLE_PROBE:
                return table
            elif argv[:2] == ["sh", "-c"]:
                stdout = HEALTHY_TAILSCALE
            elif argv[:2] == ["systemctl", "show"]:
                stdout = "active\n"
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with (
            mock.patch.object(
                yo.proc,
                "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(yo.lima, "du_kib", return_value=None),
            mock.patch.object(yo.guest, "vm_units_present", return_value=units_present),
            mock.patch.object(yo.LIMA, "config_gaps", return_value=list(gaps)),
            mock.patch.object(yo.LIMA, "host_gaps", return_value=list(host_gaps)),
            mock.patch.object(yo.shutil, "which", return_value=aws_which),
            mock.patch.object(
                yo.aws, "aws_allowlist", side_effect=aws_allowlist_side_effect
            ),
            mock.patch.object(
                yo.aws,
                "aws_broker_health",
                return_value=broker
                or yo.BrokerHealth(
                    yo.BrokerState.HEALTHY,
                    {"pid": 5, "watch_pid": keepalive_pid, "allow": ["work"]},
                ),
            ),
            mock.patch.object(yo.LIMA, "keepalive_pid", return_value=keepalive_pid),
            mock.patch.object(yo.ui, "out", side_effect=fake_out),
            mock.patch.object(yo.ui, "err"),
            mock.patch.object(yo.guest, "guest_helper", helper),
        ):
            try:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))
                self.raised = None
            except yo.YoError as failure:
                self.raised = failure
        return outs


class TestCmdStatus(StatusRunner, unittest.TestCase):
    def test_old_box_prints_units_missing_note_instead_of_proxy_and_1password_lines(
        self,
    ):
        outs = self.run_status(
            units_present=False,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertTrue(any(yo.units_missing_note() in line for line in outs))
        self.assertFalse(any(line.startswith("1password:") for line in outs))
        self.assertFalse(any(line.startswith("proxy:") for line in outs))

    def test_units_present_prints_1password_and_proxy_lines(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertTrue(any(line.startswith("1password:") for line in outs))
        self.assertTrue(any(line.startswith("proxy:") for line in outs))

    def test_lima_config_gap_is_reported(self):
        outs = self.run_status(
            units_present=True,
            gaps=[yo.OP_GUEST_SOCK],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertTrue(
            any(line.startswith("lima:") and yo.OP_GUEST_SOCK in line for line in outs)
        )

    def test_no_lima_config_gap_is_reported_as_ok(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertIn("lima: reverse forwards ok", outs)

    def test_malformed_aws_config_reports_unreadable_instead_of_raising(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=yo.YoError(
                "cannot parse ~/.aws/config: bad bytes"
            ),
        )
        self.assertTrue(any("aws: config unreadable:" in line for line in outs))

    def status_with_fstype(self, fstype):
        return self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
            fstype=fstype,
        )

    def test_an_unencrypted_root_is_flagged_with_the_remedy(self):
        outs = self.status_with_fstype("ext4\n")
        self.assertIn(
            "disk: /dev/vda2 is ext4 — NOT encrypted; rebuild and reboot: yo bootstrap",
            outs,
        )

    def test_an_encrypted_root_is_reported_plainly(self):
        outs = self.status_with_fstype("crypto_LUKS\n")
        self.assertIn("disk: /dev/vda2 is crypto_LUKS", outs)
        self.assertFalse(any("NOT encrypted" in line for line in outs))

    def test_an_unreadable_root_type_is_not_called_unencrypted(self):
        outs = self.status_with_fstype("")
        self.assertIn("disk: cannot read the type of /dev/vda2", outs)
        self.assertFalse(any("NOT encrypted" in line for line in outs))

    def test_the_lima_line_names_the_over_vsock_remedy(self):
        outs = self.run_status(
            units_present=True,
            gaps=[yo.GAP_SSH_OVER_VSOCK],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertTrue(
            any(
                line.startswith("lima:") and ".ssh.overVsock = false" in line
                for line in outs
            )
        )

    def status_of_an_initrd_box(self, probe):
        """`yo status` of a box whose initrd answers the probe with `probe`.

        Only the initrd's sshd answers, and only to root: the stage-2 login is
        refused. Any pinned ssh would meet the initrd's per-boot host key and
        print the changed-host-key warning, so one is a failure.
        """
        outs, errs, pinned_ssh = [], [], []

        def initrd_run(argv, **kwargs):
            if "User=root" in argv:
                return mock.Mock(
                    returncode=0,
                    stdout=probe if argv[-1] == yo.INITRD_PROBE_COMMAND else "",
                    stderr="",
                )
            return mock.Mock(
                returncode=yo.SSH_UNREACHABLE, stdout="", stderr="Permission denied"
            )

        def pinned_ssh_run(role, argv, **kwargs):
            pinned_ssh.append(argv)
            return mock.Mock(
                returncode=yo.SSH_UNREACHABLE,
                stdout="",
                stderr="WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!",
            )

        with (
            mock.patch.object(yo.proc, "run", side_effect=initrd_run),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=pinned_ssh_run),
            mock.patch.object(yo.LIMA, "config_gaps", return_value=[]),
            mock.patch.object(yo.lima, "du_kib", return_value=None),
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo.ui, "out", side_effect=outs.append),
            mock.patch.object(yo.ui, "err", side_effect=errs.append),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_status(yo.build_parser().parse_args(["status"]))
        self.assertEqual((caught.exception.message, caught.exception.code), (None, 1))
        self.assertEqual(pinned_ssh, [])
        self.assertEqual(errs, [])
        return [line for line in outs if line.startswith("disk: locked")]

    def test_a_box_waiting_in_its_initrd_says_it_is_locked_and_never_meets_the_pin(
        self,
    ):
        (line,) = self.status_of_an_initrd_box(PROBE_AT_THE_PASSPHRASE_PROMPT)
        self.assertIn("waiting for the passphrase", line)
        self.assertIn("yo up", line)

    def test_status_tells_a_box_encrypting_from_one_waiting_and_from_one_unreadable(
        self,
    ):
        (migrating,) = self.status_of_an_initrd_box(initrd_probe("activating"))
        self.assertIn("encrypting the disk in place", migrating)
        self.assertNotIn("waiting", migrating)
        (unknown,) = self.status_of_an_initrd_box("sh: systemctl: not found\n")
        self.assertIn("systemctl: not found", unknown)
        self.assertIn("yo down", unknown)

    def egress_lines(self, egress_probe):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
            egress_probe=egress_probe,
        )
        return [line for line in outs if line.startswith("egress:")]

    def test_a_box_whose_egress_unit_is_active_reports_the_host_blocked(self):
        self.assertEqual(
            self.egress_lines("LoadState=loaded\nActiveState=active\n"),
            ["egress: host and LAN blocked"],
        )

    def test_a_box_whose_egress_unit_failed_warns_of_recovery_mode(self):
        lines = self.egress_lines("LoadState=loaded\nActiveState=failed\n")
        self.assertEqual(len(lines), 1)
        self.assertIn("WARNING recovery table", lines[0])
        self.assertIn("(yolobox-egress failed)", lines[0])

    def test_a_box_that_predates_the_egress_unit_is_named_not_blamed(self):
        self.assertEqual(
            self.egress_lines("LoadState=not-found\nActiveState=inactive\n"),
            ["egress: box predates the egress table; run yo bootstrap"],
        )

    def test_a_silent_egress_probe_is_not_checked_not_recovery_mode(self):
        self.assertEqual(
            self.egress_lines(""),
            ["egress: not checked — the probe answered '' (exit 0)"],
        )

    def test_network_line_reports_what_the_guest_net_check_answered(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
            net_check=mock.Mock(
                returncode=0, stdout="egress=ok\ngateway=dead\n", stderr=""
            ),
        )
        self.assertIn(
            yo.network_status_line(
                yo.NetCheck(True, yo.Gateway.DEAD, yo.RootDisk.NOT_REPORTED)
            ).text,
            outs,
        )

    def test_old_box_without_net_check_reports_not_checked_instead_of_raising(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
            net_check=yo.YoError(
                "the box's yolobox-guest has no 'net-check' subcommand"
            ),
        )
        self.assertTrue(
            any(line.startswith("network: not checked — ") for line in outs)
        )
        self.assertIn("lima: reverse forwards ok", outs)

    def test_net_check_output_that_names_no_known_probe_result_reports_not_checked(
        self,
    ):
        for stdout in (
            "",
            "egress=ok\n",
            "egress=ok\ngateway=maybe\n",
            "egress=up\ngateway=ok\n",
        ):
            with self.subTest(stdout=stdout):
                outs = self.run_status(
                    units_present=True,
                    gaps=[],
                    aws_which=None,
                    aws_allowlist_side_effect=lambda: [],
                    net_check=mock.Mock(returncode=1, stdout=stdout, stderr="boom"),
                )
                self.assertTrue(
                    any(line.startswith("network: not checked — ") for line in outs)
                )


class TestCmdStatusExitCode(StatusRunner, unittest.TestCase):
    # `yo status` exits 0 only when nothing it checked is wrong, so a script
    # (tests/box-hardening-e2e.sh) can gate on it. A line that says "WARNING",
    # and the other lines the decision lists, fail the run; lines about things
    # that are optional or merely not checked do not.
    def assertFailsTheRun(self, **broken):
        outs = self.run_status(**broken)
        self.assertIsNotNone(self.raised, "status returned 0 for %r" % (broken,))
        self.assertEqual((self.raised.message, self.raised.code), (None, 1))
        return outs

    def assertPassesTheRun(self, **kwargs):
        outs = self.run_status(**kwargs)
        self.assertIsNone(self.raised)
        return outs

    def answers(self, argv_prefix, **fields):
        def answer(role, argv):
            if argv[: len(argv_prefix)] == argv_prefix:
                return mock.Mock(
                    **{"returncode": 0, "stdout": "", "stderr": "", **fields}
                )
            return None

        return answer

    def test_a_healthy_box_exits_zero(self):
        self.assertPassesTheRun()

    def test_each_wrong_thing_fails_the_run(self):
        broken = {
            "root not encrypted": dict(fstype="ext4\n"),
            "gap": dict(gaps=[yo.OP_GUEST_SOCK]),
            "host gap": dict(host_gaps=["the passt socket directory is read-only"]),
            "units missing": dict(units_present=False),
            "1password forward down": dict(
                ssh_answer=self.answers(["ssh-add"], returncode=2)
            ),
            "proxy unit failed": dict(
                ssh_answer=self.answers(["systemctl", "show"], stdout="failed\n")
            ),
            "proxy unit unknown": dict(
                ssh_answer=self.answers(["systemctl", "show"], stdout="")
            ),
            "disk full": dict(
                ssh_answer=self.answers(
                    yo.guest.ROOT_USAGE_PROBE,
                    stdout=df_output(human_line="/dev/vda2 59G 55G 3G 95% /"),
                )
            ),
            "tailscale stopped": dict(
                ssh_answer=self.answers(
                    ["sh", "-c"], stdout=json.dumps({"BackendState": "Stopped"})
                )
            ),
            "tailscale unreadable": dict(
                ssh_answer=self.answers(["sh", "-c"], stdout="", stderr="boom")
            ),
            "egress unit failed": dict(
                egress_probe="LoadState=loaded\nActiveState=failed\n"
            ),
            "egress masked": dict(
                egress_probe="LoadState=masked\nActiveState=inactive\n"
            ),
            "egress predates": dict(
                egress_probe="LoadState=not-found\nActiveState=inactive\n"
            ),
            "egress active over a wrong table": dict(
                table=mock.Mock(returncode=0, stdout=FIXTURE_EMPTY_TABLE, stderr="")
            ),
            "network dead": dict(
                net_check=mock.Mock(
                    returncode=0, stdout="egress=dead\ngateway=ok\n", stderr=""
                )
            ),
            "aws config unreadable": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=yo.YoError("cannot parse ~/.aws/config"),
            ),
            "aws guest forward down": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                ssh_answer=self.answers(["curl"], returncode=7),
            ),
            "aws broker unreachable": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                broker=yo.BROKER_UNREACHABLE,
            ),
            "aws broker stale": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
                broker=yo.BrokerHealth(
                    yo.BrokerState.HEALTHY, {"pid": 5, "watch_pid": 1234, "allow": []}
                ),
            ),
        }
        for name, kwargs in broken.items():
            with self.subTest(name=name):
                self.assertFailsTheRun(**kwargs)

    def test_a_host_gap_is_a_warning_line_naming_the_driver(self):
        outs = self.assertFailsTheRun(
            host_gaps=["the passt socket directory is read-only"]
        )
        self.assertIn("lima: WARNING the passt socket directory is read-only", outs)

    def test_a_use_percentage_that_is_not_ascii_digits_is_not_a_number(self):
        # "²".isdigit() is True and int("²") raises.
        outs = self.assertPassesTheRun(
            ssh_answer=self.answers(
                yo.guest.ROOT_USAGE_PROBE,
                stdout=df_output(human_line="/dev/vda2 59G 55G 3G \u00b2% /"),
            )
        )
        self.assertFalse(any("full" in line for line in outs))

    def test_a_df_that_fails_is_a_soft_status_line_naming_what_it_said(self):
        outs = self.assertPassesTheRun(
            ssh_answer=self.answers(
                yo.guest.ROOT_USAGE_PROBE, returncode=1, stderr="df: /: gone"
            )
        )
        self.assertIn(
            "disk: guest cannot read the VM's root usage from df (exit 1): df: /: gone",
            outs,
        )

    def test_version_drift_fails_the_run(self):
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            self.assertFailsTheRun(ssh_answer=self.answers(["cat"], stdout="1.0.0\n"))

    def test_a_box_built_by_this_yo_does_not_drift(self):
        with mock.patch.object(yo.config, "YO_VERSION", "1.2.3"):
            self.assertPassesTheRun(ssh_answer=self.answers(["cat"], stdout="1.2.3\n"))

    def test_optional_or_unchecked_things_do_not_fail_the_run(self):
        optional = {
            "tailscale not logged in": dict(
                ssh_answer=self.answers(
                    ["sh", "-c"], stdout=json.dumps({"BackendState": "NeedsLogin"})
                )
            ),
            "box without tailscale": dict(
                ssh_answer=self.answers(["sh", "-c"], stdout="NO_TAILSCALE\n")
            ),
            "network not checked on an old box": dict(
                net_check=yo.YoError("the box's yolobox-guest has no 'net-check'")
            ),
            "egress not checked": dict(egress_probe=""),
            "root type unreadable": dict(fstype=""),
            "aws disabled": dict(aws_which=None),
            "aws healthy": dict(
                aws_which="/usr/bin/aws",
                aws_allowlist_side_effect=lambda: [("work", "us-east-1")],
            ),
        }
        for name, kwargs in optional.items():
            with self.subTest(name=name):
                self.assertPassesTheRun(**kwargs)

    def egress_line(self, **kwargs):
        outs = self.run_status(**kwargs)
        (line,) = [line for line in outs if line.startswith("egress:")]
        return line

    def test_a_failed_unit_names_which_fallback_table_the_kernel_holds(self):
        failed = "LoadState=loaded\nActiveState=failed\n"
        tiers = {
            "recovery table": "table inet yolobox-egress {\n\tchain output_recovery {\n",
            "last-resort table": "table inet yolobox-egress {\n\tchain output_lastresort {\n",
            # a box built before the tiers existed called its recovery chain "output"
            "recovery table (old)": "table inet yolobox-egress {\n\tchain output {\n",
        }
        for expected, listing in tiers.items():
            with self.subTest(expected=expected):
                line = self.egress_line(
                    egress_probe=failed,
                    table=mock.Mock(returncode=0, stdout=listing, stderr=""),
                )
                self.assertTrue(
                    line.startswith("egress: WARNING %s" % expected.split(" (")[0]),
                    line,
                )
        self.assertIsNotNone(self.raised)

    def test_the_chain_name_is_compared_whole_not_by_prefix(self):
        # "chain output_lastresort {" begins with "chain output" and must not be
        # read as the full table's chain, and a chain of another name that merely
        # begins the same way is not any tier.
        failed = "LoadState=loaded\nActiveState=failed\n"
        for chain in (
            "chain output_lastresort2 {",
            "chain output2 {",
            "chain outputs {",
        ):
            with self.subTest(chain=chain):
                line = self.egress_line(
                    egress_probe=failed,
                    table=mock.Mock(returncode=0, stdout="\t%s\n" % chain, stderr=""),
                )
                self.assertIn("none of the three tiers", line)

    def test_a_failed_unit_with_no_table_is_a_fail_open_warning(self):
        line = self.egress_line(
            egress_probe="LoadState=loaded\nActiveState=failed\n",
            table=mock.Mock(
                returncode=1, stdout="", stderr="Error: No such file or directory\n"
            ),
        )
        self.assertTrue(line.startswith("egress: WARNING no table (fail-open)"), line)
        self.assertIsNotNone(self.raised)

    def test_a_failed_unit_whose_table_cannot_be_read_says_so(self):
        line = self.egress_line(
            egress_probe="LoadState=loaded\nActiveState=failed\n",
            table=mock.Mock(
                returncode=1, stdout="", stderr="sudo: a password is required\n"
            ),
        )
        self.assertIn("could not be read: sudo: a password is required", line)

    def test_losing_the_box_midway_fails_the_run_and_still_reports_the_rest(self):
        def lost(role, argv):
            if argv[:1] == ["cat"]:
                return mock.Mock(returncode=yo.SSH_UNREACHABLE, stdout="", stderr="")
            return None

        outs = self.assertFailsTheRun(ssh_answer=lost)
        self.assertTrue(any(line.startswith("vm: lost while probing") for line in outs))
        self.assertIn("lima: reverse forwards ok", outs)


class TestGuestNetworkLine(unittest.TestCase):
    def test_net_check_runs_as_the_operator_with_no_arguments(self):
        helper = mock.Mock(
            return_value=mock.Mock(returncode=0, stdout="egress=ok\ngateway=ok\n")
        )
        with mock.patch.object(yo.guest, "guest_helper", helper):
            line = yo.guest_network_line()
        helper.assert_called_once_with(yo.OPERATOR, "net-check", [])
        self.assertEqual(line, yo.StatusLine("network: ok"))

    def test_a_box_that_still_wants_a_port_reports_not_checked(self):
        helper = mock.Mock(
            return_value=mock.Mock(returncode=2, stdout="", stderr="usage: net-check")
        )
        with mock.patch.object(yo.guest, "guest_helper", helper):
            line = yo.guest_network_line()
        self.assertTrue(line.text.startswith("network: not checked — "))
        self.assertFalse(line.warning)

    def test_a_failed_net_check_shows_why(self):
        helper = mock.Mock(
            return_value=mock.Mock(
                returncode=1,
                stdout="",
                stderr="yolobox-guest: stat -f / answered 'x', not two numbers\n",
            )
        )
        with mock.patch.object(yo.guest, "guest_helper", helper):
            line = yo.guest_network_line()
        self.assertEqual(
            line.text,
            "network: not checked — net-check answered '' (exit 1): "
            "yolobox-guest: stat -f / answered 'x', not two numbers",
        )

    def test_a_lost_box_is_not_a_not_checked_line(self):
        helper = mock.Mock(side_effect=yo.sshx.unreachable(yo.OPERATOR))
        with mock.patch.object(yo.guest, "guest_helper", helper):
            with self.assertRaises(yo.Unreachable):
                yo.guest_network_line()


class TestNetworkStatusLine(unittest.TestCase):
    def test_both_ok(self):
        self.assertEqual(
            yo.network_status_line(yo.NetCheck(True, yo.Gateway.OK, yo.RootDisk.OK)),
            yo.StatusLine("network: ok"),
        )

    def test_dead_egress_with_answering_gateway_blames_a_default_route_vpn(self):
        line = yo.network_status_line(yo.NetCheck(False, yo.Gateway.OK, yo.RootDisk.OK))
        self.assertEqual(
            line,
            yo.StatusLine(
                "network: WARNING guest egress is dead while lima's gateway answers DNS — "
                "a VPN on the host likely owns its default route",
                True,
            ),
        )

    def test_a_silent_gateway_with_working_egress_blames_the_hosts_vpn_or_dns_filter(
        self,
    ):
        line = yo.network_status_line(
            yo.NetCheck(True, yo.Gateway.DEAD, yo.RootDisk.OK)
        )
        self.assertEqual(
            line,
            yo.StatusLine(
                "network: WARNING lima's gateway does not answer DNS while egress works — "
                "DNS through the gateway will fail; check the host's VPN or DNS filter",
                True,
            ),
        )

    def test_both_dead_points_at_the_lima_line(self):
        line = yo.network_status_line(
            yo.NetCheck(False, yo.Gateway.DEAD, yo.RootDisk.OK)
        )
        self.assertEqual(
            line,
            yo.StatusLine(
                "network: WARNING the guest reaches neither the internet nor lima's gateway DNS — "
                "check yo status's lima line and the host's VPN",
                True,
            ),
        )

    def test_a_full_root_disk_is_named_whatever_the_probes_blame(self):
        for egress_ok in (True, False):
            for gateway in yo.Gateway:
                with self.subTest(egress_ok=egress_ok, gateway=gateway):
                    line = yo.network_status_line(
                        yo.NetCheck(egress_ok, gateway, yo.RootDisk.FULL)
                    )
                    self.assertTrue(line.warning)
                    self.assertIn("root disk is full", line.text)
                    self.assertIn("`yo gc --yes`", line.text)
                    self.assertNotIn("VPN", line.text)

    def test_a_full_root_disk_reports_what_the_probes_saw_not_a_certain_failure(self):
        line = yo.network_status_line(
            yo.NetCheck(True, yo.Gateway.OK, yo.RootDisk.FULL)
        )
        self.assertIn("can fail", line.text)
        self.assertNotIn("name resolution fails", line.text)
        self.assertIn("egress ok", line.text)
        self.assertIn("gateway ok", line.text)
        line = yo.network_status_line(
            yo.NetCheck(False, yo.Gateway.DEAD, yo.RootDisk.FULL)
        )
        self.assertIn("egress dead", line.text)
        self.assertIn("gateway dead", line.text)

    def test_net_check_output_reads_rootdisk_and_tells_a_box_without_it_apart(self):
        for text, root_disk in (
            ("egress=dead\ngateway=ok\nrootdisk=full\n", yo.RootDisk.FULL),
            ("egress=ok\ngateway=ok\nrootdisk=ok\n", yo.RootDisk.OK),
            ("egress=ok\ngateway=ok\n", yo.RootDisk.NOT_REPORTED),
        ):
            with self.subTest(text=text):
                self.assertEqual(yo.parse_net_check(text).root_disk, root_disk)

    def test_a_box_that_does_not_report_its_disk_gets_no_disk_verdict(self):
        line = yo.network_status_line(
            yo.NetCheck(True, yo.Gateway.OK, yo.RootDisk.NOT_REPORTED)
        )
        self.assertEqual(line, yo.StatusLine("network: ok"))

    def test_a_rootdisk_word_net_check_never_prints_is_not_checked(self):
        self.assertIsNone(
            yo.parse_net_check("egress=ok\ngateway=ok\nrootdisk=lizard\n")
        )

    def test_a_blocked_gateway_with_working_egress_says_the_probe_was_skipped(self):
        self.assertEqual(
            yo.network_status_line(
                yo.NetCheck(True, yo.Gateway.SKIPPED, yo.RootDisk.OK)
            ),
            yo.StatusLine(
                "network: ok (lima's gateway probe skipped: this box blocks it by design)"
            ),
        )

    def test_a_blocked_gateway_with_dead_egress_warns_without_blaming_a_vpn(
        self,
    ):
        self.assertEqual(
            yo.network_status_line(
                yo.NetCheck(False, yo.Gateway.SKIPPED, yo.RootDisk.OK)
            ),
            yo.StatusLine(
                "network: WARNING guest egress is dead "
                "(lima's gateway probe skipped: this box blocks it by design)",
                True,
            ),
        )


class FakeStarter:
    """A `limactl start` process: running until waited on, or already exited."""

    def __init__(self, code, exited_already):
        self.code = code
        self.exited_already = exited_already
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.code if self.exited_already or self.waited else None

    @property
    def returncode(self):
        return self.poll()

    def wait(self):
        self.waited = True
        return self.code

    def terminate(self):
        self.terminated = True


class RecordedStdin:
    def __init__(self):
        self.sent = b""

    def write(self, data):
        self.sent += data

    def close(self):
        pass


class FakeHelper:
    """The `yolobox-unlock` ssh session: scripted stdout lines, exit code, stderr."""

    def __init__(self, lines, code, stderr):
        self.stdout = io.BytesIO("".join(line + "\n" for line in lines).encode())
        self.stderr = io.BytesIO(stderr.encode())
        self.stdin = RecordedStdin()
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self):
        return self.code


NOT_LISTENING = (255, "ssh: connect to host 127.0.0.1 port 60022: Connection refused")
REFUSED = (255, "root@127.0.0.1: Permission denied (publickey).")
ANSWERS = (0, "")
UNLOCKED = (["yolobox-unlock: unlocked"], 0, "")
MIGRATE_ASKED = "yolobox-unlock: answered id=%s" % yo.MIGRATE_ASK_ID
MIGRATED_AND_UNLOCKED = ([MIGRATE_ASKED, "yolobox-unlock: unlocked"], 0, "")
DROPPED_AFTER_MIGRATING = ([MIGRATE_ASKED], 255, "")
NOT_LISTENING_HELPER = ([], *NOT_LISTENING)
REFUSED_HELPER = ([], *REFUSED)


class FakeBox:
    """Lima and the guest's two sshds as the Mac sees them, at run and Popen.

    Each script is a list consumed front to back whose last entry repeats.
    Time is virtual: sleeping advances the clock the deadlines read.
    """

    def __init__(
        self,
        test,
        *,
        statuses=("Running",),
        operator=(ANSWERS,),
        root=(ANSWERS,),
        helpers=(UNLOCKED,),
        starts=((0, False),),
    ):
        self.statuses = list(statuses)
        self.operator = list(operator)
        self.root = list(root)
        self.helper_script = list(helpers)
        self.start_script = list(starts)
        self.clock = 1000.0
        self.sleeps = []
        self.stops = 0
        self.starts = []
        self.helpers = []
        self.running_since = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.luks_passphrase = mock.Mock(return_value=b"a" * 64)
        self.vm_services = mock.Mock()
        home = tempfile.TemporaryDirectory()
        test.addCleanup(home.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"HOME": home.name}),
            mock.patch.object(yo.proc, "run", self.run),
            mock.patch.object(yo.subprocess, "Popen", self.popen),
            mock.patch.object(yo.agent, "luks_passphrase", self.luks_passphrase),
            mock.patch.object(yo.forwards, "vm_services", self.vm_services),
            mock.patch.object(yo.sshconfig, "ensure_agent_ssh_config"),
            mock.patch.object(yo.guest, "ensure_guest_known_hosts"),
            mock.patch.object(yo.LIMA, "config_gaps", return_value=[]),
            mock.patch.object(yo.time, "sleep", self.sleep),
            mock.patch.object(yo.time, "monotonic", lambda: self.clock),
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ]
        for patch in patches:
            patch.__enter__()
            test.addCleanup(patch.__exit__, None, None, None)

    @staticmethod
    def next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 2000:
            raise AssertionError("yo is polling forever")
        self.clock += seconds

    def run(self, argv, **kwargs):
        argv = list(argv)
        if argv[:3] == ["limactl", "list", "yolobox"]:
            status = self.next(self.statuses)
            if status == "Running" and self.starts and self.running_since is None:
                self.running_since = self.clock
            payload = json.dumps({"status": status, "config": {}})
            return subprocess.CompletedProcess(argv, 0, payload, "")
        if argv[:3] == ["limactl", "list", "-q"]:
            return subprocess.CompletedProcess(argv, 0, "yolobox\n", "")
        if argv[:2] == ["limactl", "stop"]:
            self.stops += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh" and argv[-1] == "true":
            user = next(a for a in argv if a.startswith("User=")).split("=", 1)[1]
            script = self.root if user == "root" else self.operator
            code, stderr = self.next(script)
            return subprocess.CompletedProcess(argv, code, "", stderr)
        raise AssertionError("unexpected run: %r" % argv)

    def popen(self, argv, **kwargs):
        argv = list(argv)
        if argv[:2] == ["limactl", "start"]:
            starter = FakeStarter(*self.next(self.start_script))
            self.starts.append(starter)
            return starter
        if argv[-1] == yo.INITRD_UNLOCK_HELPER:
            helper = FakeHelper(*self.next(self.helper_script))
            self.helpers.append(helper)
            return helper
        raise AssertionError("unexpected Popen: %r" % argv)

    def up(self):
        args = yo.build_parser().parse_args(["up"])
        args.func(args)


class TestUpStartsAndUnlocks(unittest.TestCase):
    def test_a_box_that_is_up_needs_no_passphrase_and_no_start(self):
        box = FakeBox(self, statuses=["Running"], operator=[ANSWERS])
        box.up()
        box.luks_passphrase.assert_not_called()
        self.assertEqual(box.starts, [])
        self.assertEqual(box.helpers, [])
        box.vm_services.assert_called_once_with()

    def test_a_stopped_box_is_started_and_unlocked_with_the_derived_passphrase(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[UNLOCKED])
        box.up()
        self.assertEqual(len(box.starts), 1)
        self.assertEqual(len(box.helpers), 1)
        self.assertEqual(box.helpers[0].stdin.sent, b"a" * 64 + b"\n")
        self.assertIn("yolobox-unlock: unlocked", box.stdout.getvalue())
        box.vm_services.assert_called_once_with()

    def test_a_helper_warning_on_a_successful_unlock_reaches_the_operator(self):
        warning = "yolobox-unlock: reply to 7 failed"
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[(["yolobox-unlock: unlocked"], 0, warning)],
        )
        box.up()
        self.assertIn(warning, box.stderr.getvalue())

    def test_a_helper_warning_before_the_session_dropped_reaches_the_operator(self):
        warning = "yolobox-unlock: ask names no socket, skipped"
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING, ANSWERS],
            helpers=[([MIGRATE_ASKED], 255, warning)],
        )
        box.up()
        self.assertIn(warning, box.stderr.getvalue())

    def test_a_box_waiting_in_its_initrd_is_unlocked_without_a_start(self):
        box = FakeBox(self, operator=[REFUSED, ANSWERS], root=[ANSWERS])
        box.up()
        self.assertEqual(box.starts, [])
        self.assertEqual(len(box.helpers), 1)
        box.luks_passphrase.assert_called_once_with()

    def test_a_running_box_that_answers_nowhere_is_an_error_without_a_prompt(self):
        box = FakeBox(self, operator=[NOT_LISTENING], root=[NOT_LISTENING])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("neither its initrd nor its sshd", caught.exception.message)
        box.luks_passphrase.assert_not_called()

    def test_a_migration_after_lima_gave_up_restarts_cleanly_once(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[MIGRATED_AND_UNLOCKED, UNLOCKED],
            starts=[(1, False), (0, False)],
        )
        box.up()
        self.assertEqual(box.stops, 1)
        self.assertEqual(len(box.starts), 2)
        self.assertEqual(len(box.helpers), 2)
        box.vm_services.assert_called_once_with()

    def test_lima_failing_without_a_migration_is_not_restarted(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[UNLOCKED],
            starts=[(1, False)],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("limactl start exited 1", caught.exception.message)
        self.assertEqual(box.stops, 0)

    def test_a_failed_migration_says_the_plaintext_is_intact_and_ends_the_start(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[(["e2fsck failed"], 2, "no space")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("plaintext intact", caught.exception.message)
        self.assertIn("no space", caught.exception.message)
        self.assertTrue(box.starts[0].terminated)

    def test_a_wrong_key_is_named_and_there_is_no_recovery_slot(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 1, "")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("no recovery slot", caught.exception.message)

    def test_a_lost_watch_in_the_helper_is_reported(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 3, "lost")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("lost its watch", caught.exception.message)

    def test_a_root_mount_that_failed_is_reported_at_once_naming_sysroot_mount(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[([], 5, "yolobox-unlock: sysroot.mount failed")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        message = caught.exception.message
        self.assertIn("sysroot.mount failed", message)
        self.assertIn("journalctl -b", message)
        # The remedy is the command yo itself runs, not a second spelling of it.
        self.assertIn(shlex.join(yo.initrd_ssh_argv("journalctl -b")), message)

    def test_an_empty_passphrase_at_the_helper_is_reported_as_such(self):
        box = FakeBox(self, statuses=["Stopped", "Running"], helpers=[([], 4, "")])
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("no passphrase reached", caught.exception.message)

    def test_a_vm_that_stops_while_nothing_is_starting_it_is_an_error_not_a_wait(
        self,
    ):
        box = FakeBox(
            self,
            statuses=["Running", "Running", "Stopped"],
            operator=[NOT_LISTENING],
            root=[ANSWERS],
            helpers=[NOT_LISTENING_HELPER],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("is Stopped, not Running", caught.exception.message)
        self.assertLessEqual(len(box.sleeps), 2)

    def test_a_plain_boot_whose_lima_start_already_succeeded_needs_no_unlock(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[NOT_LISTENING_HELPER],
            starts=[(0, True)],
        )
        box.up()
        self.assertEqual(len(box.starts), 1)
        box.vm_services.assert_called_once_with()

    def test_a_finished_lima_start_with_a_silent_operator_waits_for_the_deadline(
        self,
    ):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING],
            helpers=[NOT_LISTENING_HELPER],
            starts=[(0, True)],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("never offered its initrd sshd", caught.exception.message)
        self.assertGreater(sum(box.sleeps), yo.INITRD_WAIT_SECONDS)

    def test_an_unexpected_helper_exit_carries_its_stderr(self):
        box = FakeBox(
            self, statuses=["Stopped", "Running"], helpers=[([], 17, "weird")]
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("exit 17", caught.exception.message)
        self.assertIn("weird", caught.exception.message)

    def test_an_ssh_failure_of_no_known_shape_is_surfaced_not_retried(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[([], 255, "Bad configuration option: lizard")],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("lizard", caught.exception.message)
        self.assertEqual(box.sleeps, [])

    def test_a_key_refused_for_root_and_the_operator_names_cidata_at_the_deadline(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[REFUSED],
            helpers=[REFUSED_HELPER],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("cidata", caught.exception.message)
        self.assertGreater(sum(box.sleeps), yo.INITRD_WAIT_SECONDS)

    def test_a_refusal_that_clears_when_the_operators_key_lands_is_no_error(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[REFUSED, ANSWERS],
            helpers=[REFUSED_HELPER],
        )
        box.up()
        self.assertEqual(len(box.starts), 1)

    def test_lima_exiting_before_the_initrd_answers_is_reported_at_once(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            helpers=[NOT_LISTENING_HELPER],
            starts=[(1, True)],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("limactl start exited 1", caught.exception.message)
        self.assertLessEqual(len(box.sleeps), 1)

    def test_the_deadline_starts_only_when_lima_reports_running(self):
        installing = 400  # 400 polls of 2 s would already pass the 600 s budget
        box = FakeBox(
            self,
            statuses=["Stopped"] + ["Installing"] * installing + ["Running"],
            helpers=[NOT_LISTENING_HELPER],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("never offered its initrd sshd", caught.exception.message)
        self.assertGreaterEqual(len(box.sleeps), installing)
        waited = box.clock - box.running_since
        self.assertGreater(waited, yo.INITRD_WAIT_SECONDS)
        self.assertLess(waited, yo.INITRD_WAIT_SECONDS + 10)

    def test_an_unlocked_disk_whose_operator_never_answers_is_an_error(self):
        box = FakeBox(
            self,
            statuses=["Stopped", "Running"],
            operator=[NOT_LISTENING],
            helpers=[UNLOCKED],
        )
        with self.assertRaises(yo.YoError) as caught:
            box.up()
        self.assertIn("not reachable as the operator", caught.exception.message)
        self.assertTrue(box.starts[0].terminated)


class TestUnlockSurvivesADroppedSession(unittest.TestCase):
    # The migration streamed for longer than lima's 10 minutes, so limactl start
    # exited 1 long ago and the session then dropped at switch-root; stage 2's
    # sshd refuses root, and the operator answers only a few probes later.
    SCRIPT = dict(
        statuses=["Stopped", "Running"],
        operator=[NOT_LISTENING, NOT_LISTENING, NOT_LISTENING, ANSWERS],
        helpers=[
            DROPPED_AFTER_MIGRATING,
            REFUSED_HELPER,
            REFUSED_HELPER,
            REFUSED_HELPER,
            UNLOCKED,
        ],
    )

    def test_lima_having_exited_is_not_blamed_once_the_session_has_run(self):
        FakeBox(self, **dict(self.SCRIPT, statuses=["Running"]))
        migrated = yo.unlock_disk(b"a" * 64, FakeStarter(1, True))
        self.assertTrue(migrated)

    def test_up_then_stops_and_starts_lima_cleanly(self):
        box = FakeBox(self, starts=[(1, True), (0, False)], **self.SCRIPT)
        box.up()
        self.assertEqual(box.stops, 1)
        self.assertEqual(len(box.starts), 2)
        self.assertEqual(len(box.helpers), 5)
        box.vm_services.assert_called_once_with()


class TestRunTimeout(unittest.TestCase):
    def test_a_command_over_its_timeout_is_an_error_naming_it(self):
        expired = subprocess.TimeoutExpired(["limactl", "stop"], 5)
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                yo.run(["limactl", "stop", "yolobox"], timeout=5)
        self.assertIn(
            "limactl stop yolobox did not finish within 5 s", caught.exception.message
        )


PROBE_AT_THE_PASSPHRASE_PROMPT = initrd_probe("active", ["cryptsetup:/dev/vda2"])
LIMA_FORCE_STOP = "limactl stop --force yolobox"
# An initrd box the probe cannot prove idle: what the probe prints, the words
# `yo down` must answer with, and whether it offers the force-stop command.
NOT_IDLE_PROBES = {
    "migrating": (initrd_probe("activating"), "encrypting the disk in place", False),
    "unlocking": (
        initrd_probe("active", ["cryptsetup:/dev/vda2"], unlock_running=True),
        "leftover",
        True,
    ),
    "a failed migration": (
        initrd_probe("failed", ["cryptsetup:/dev/vda2"]),
        "in-place disk encryption failed",
        True,
    ),
    "an unlocked box nothing asks anything of": (
        initrd_probe("active"),
        "sysroot.mount failed",
        True,
    ),
    "a shell that did not run the probe": (
        "sh: systemctl: not found\n",
        "cannot prove it is idle",
        True,
    ),
}


class TestLimaStop(FakeHome):
    def test_a_hung_stop_carries_the_recovery(self):
        expired = subprocess.TimeoutExpired(["limactl", "stop"], yo.LIMA_STOP_TIMEOUT)
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                yo.LIMA.stop()
        self.assertIn(
            "did not finish within %d s" % yo.LIMA_STOP_TIMEOUT,
            caught.exception.message,
        )
        self.assertIn("kill -9", caught.exception.message)
        self.assertIn("systemctl reboot", caught.exception.message)

    def test_a_hung_force_stop_does_not_send_the_operator_into_the_guest(self):
        # A box in its initrd has no guest to ssh into: only the hostagent's kill helps.
        expired = subprocess.TimeoutExpired(
            ["limactl", "stop", "--force"], yo.LIMA_STOP_TIMEOUT
        )
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                yo.LIMA.force_stop()
        self.assertIn("kill -9", caught.exception.message)
        self.assertIn("limactl hostagent", caught.exception.message)
        self.assertNotIn("systemctl reboot", caught.exception.message)

    def test_a_stop_that_failed_for_another_reason_is_not_called_a_hang(self):
        failed = subprocess.CompletedProcess(["limactl", "stop"], 1, None, None)
        with mock.patch.object(yo.subprocess, "run", return_value=failed):
            with self.assertRaises(yo.YoError) as caught:
                yo.LIMA.stop()
        self.assertIn("failed", caught.exception.message)
        self.assertNotIn("kill -9", caught.exception.message)

    def assert_unlock_lock_held(self):
        with self.assertRaises(yo.UnlockBusy):
            with yo.unlock_lock(wait=False):
                pass

    def down(self, stage, probe="", ssh_result=None):
        """Run `yo down`; the initrd, when asked, answers `probe` to the ssh probe.

        Every process `yo down` starts in the initrd must find the box's unlock
        lock held.
        """
        started = self.started = []

        def answer(argv, **kwargs):
            if stage is yo.BootStage.INITRD:
                self.assert_unlock_lock_held()
            started.append(argv)
            if argv[0] == "ssh":
                self.assertEqual(argv[-1], yo.INITRD_PROBE_COMMAND)
                return ssh_result or subprocess.CompletedProcess(argv, 0, probe, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch.object(yo.unlock, "boot_stage", return_value=stage),
            mock.patch.object(yo.proc, "run", side_effect=answer) as fake_run,
            mock.patch.object(yo.ui, "out"),
        ):
            args = yo.build_parser().parse_args(["down"])
            args.func(args)
        return fake_run

    def refused_down(self, probe="", ssh_result=None):
        with self.assertRaises(yo.YoError) as caught:
            self.down(yo.BootStage.INITRD, probe, ssh_result)
        self.assertNotIn("limactl", [argv[0] for argv in self.started])
        return caught.exception.message

    def test_down_stops_the_instance_under_the_timeout(self):
        for stage in (yo.BootStage.STAGE2, yo.BootStage.NONE):
            with self.subTest(stage=stage):
                self.down(stage).assert_called_once_with(
                    ["limactl", "stop", "yolobox"],
                    capture=False,
                    check=True,
                    timeout=yo.LIMA_STOP_TIMEOUT,
                )

    def test_down_forces_the_stop_of_a_box_idle_at_the_passphrase_prompt(self):
        # The initrd has no ACPI handler: a polite stop would wait out the timeout.
        fake_run = self.down(yo.BootStage.INITRD, PROBE_AT_THE_PASSPHRASE_PROMPT)
        fake_run.assert_called_with(
            ["limactl", "stop", "--force", "yolobox"],
            capture=False,
            check=True,
            timeout=yo.LIMA_STOP_TIMEOUT,
        )

    def test_down_never_stops_a_box_that_is_not_provably_idle_in_its_initrd(self):
        # A force here is a power cut mid-encryption, and a polite stop would
        # only hang: the box is left running, and the error says why.
        for name, (probe, reason, _) in NOT_IDLE_PROBES.items():
            with self.subTest(name=name):
                self.assertIn(reason, self.refused_down(probe))

    def test_down_prints_the_force_command_where_a_box_may_be_stuck_for_good(self):
        # The refusal must never be a dead end (a leftover unlock helper, a failed
        # migration, an initrd that cannot mount the root), but a box encrypting
        # in place gets no such command: nothing makes the kill safe.
        for name, (probe, _, offers_force) in NOT_IDLE_PROBES.items():
            with self.subTest(name=name):
                message = self.refused_down(probe)
                self.assertEqual(LIMA_FORCE_STOP in message, offers_force)
                if offers_force:
                    self.assertRegex(message, r"(?i)safe")

    def test_down_never_stops_a_box_whose_initrd_probe_failed_and_says_why(self):
        failed = subprocess.CompletedProcess(["ssh"], 255, "", "boom: no route")
        message = self.refused_down(ssh_result=failed)
        self.assertIn("exited 255", message)
        self.assertIn("boom: no route", message)

    def test_down_neither_waits_for_nor_probes_a_box_another_yo_is_unlocking(self):
        with yo.unlock_lock(wait=True):
            with mock.patch.object(yo.proc, "run") as fake_run:
                with (
                    mock.patch.object(
                        yo.unlock, "boot_stage", return_value=yo.BootStage.INITRD
                    ),
                    self.assertRaisesRegex(yo.YoError, "another yo is unlocking"),
                ):
                    args = yo.build_parser().parse_args(["down"])
                    args.func(args)
        fake_run.assert_not_called()

    def test_the_lock_is_free_again_after_down(self):
        self.down(yo.BootStage.INITRD, PROBE_AT_THE_PASSPHRASE_PROMPT)
        with yo.unlock_lock(wait=False):
            pass

    def test_sending_the_passphrase_holds_the_lock(self):
        # The send is the other half of the exclusion with `yo down`.
        held = []

        def spawn(argv, **kwargs):
            try:
                with yo.unlock_lock(wait=False):
                    held.append(False)
            except yo.UnlockBusy:
                held.append(True)
            return FakeHelper(*UNLOCKED)

        with (
            mock.patch.object(yo.proc, "spawn", side_effect=spawn),
            mock.patch.object(yo.LIMA, "verify_initrd_listener"),
            mock.patch.object(yo.sshconfig, "initrd_endpoint"),
            mock.patch.object(yo.sshconfig, "initrd_ssh_argv", return_value=["ssh"]),
        ):
            yo.run_initrd_helper(b"secret")
        self.assertEqual(held, [True])


class TestDiskGrowResizesTheMapping(FakeHome):
    GROWN = (
        df_output("/dev/mapper/root 100 50 50 50% /"),
        df_output("/dev/mapper/root 200 50 150 25% /"),
    )
    UNGROWN = (
        df_output("/dev/mapper/root 200 50 150 25% /"),
        df_output("/dev/mapper/root 200 50 150 25% /"),
    )

    # A 100 GiB disk: vda2 is all of it but the 249 MiB ESP.
    FULL_VDA2 = 100 * 1024**3 - 249 * 1024**2

    # What `lsblk -no SIZE /dev/vda2` answers, as (exit code, stdout, stderr).
    vda2_listing = (0, "199.8G\n", "")

    def disk_grow(
        self, fstype="crypto_LUKS\n", size="200", df_k=GROWN, vda2_bytes=FULL_VDA2
    ):
        os.makedirs(os.path.join(self.home, ".lima"))
        calls = []
        df_k = iter(df_k)
        self.out = out = []

        self.limactl = limactl = []

        def fake_run(argv, **kwargs):
            limactl.append(list(argv))
            if argv[:2] == ["limactl", "list"]:
                return mock.Mock(returncode=0, stdout=str(100 * 1024**3), stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_ssh_run(role, argv, **kwargs):
            calls.append((argv, kwargs.get("stdin")))
            stdout = ""
            if argv[:2] == ["systemctl", "show"]:
                stdout = "loaded\n"
            elif argv[:2] == ["lsblk", "-dno"] and argv[2] == "FSTYPE":
                stdout = fstype
            elif argv == yo.guest.ROOT_USAGE_PROBE:
                stdout = next(df_k)
            elif argv[:3] == ["lsblk", "-bdno", "SIZE"]:
                stdout = "%s\n" % vda2_bytes
            elif argv[:2] == ["lsblk", "-no"]:
                code, stdout, stderr = self.vda2_listing
                return mock.Mock(returncode=code, stdout=stdout, stderr=stderr)
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        order = []
        with (
            mock.patch.object(yo.proc, "run", side_effect=fake_run),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(
                yo.LIMA, "stop", side_effect=lambda: order.append("stop")
            ),
            mock.patch.object(
                yo.vm, "vm_up", side_effect=lambda **kw: order.append(kw)
            ) as up,
            mock.patch.object(
                yo.agent,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"p" * 64,
            ),
            mock.patch.object(yo.ui, "out", side_effect=out.append),
        ):
            try:
                yo.cmd_disk_grow(yo.build_parser().parse_args(["disk-grow", size]))
            finally:
                self.order = order
                self.up = up
        return calls

    def test_grows_the_mapping_with_the_passphrase_then_the_filesystem(self):
        calls = self.disk_grow()
        resize = (
            ["sudo", "cryptsetup", "resize", "--key-file", "-", "root"],
            b"p" * 64,
        )
        grow_fs = (["sudo", "resize2fs", "/dev/mapper/root"], None)
        self.assertIn(resize, calls)
        self.assertEqual(calls.index(grow_fs), calls.index(resize) + 1)

    def test_the_passphrase_is_derived_before_the_vm_stops_and_reused_for_the_start(
        self,
    ):
        self.disk_grow()
        self.assertEqual(self.order[0], "passphrase")
        self.assertEqual(self.order[1], "stop")
        self.assertEqual(self.order[2], {"passphrase": b"p" * 64})

    def test_asking_for_the_current_ceiling_finishes_a_failed_grow_without_editing_lima(
        self,
    ):
        # `limactl edit --disk` cannot be repeated, so a grow that failed after
        # it is finished by asking for the ceiling the instance already has.
        calls = self.disk_grow(size="100")
        resize = (
            ["sudo", "cryptsetup", "resize", "--key-file", "-", "root"],
            b"p" * 64,
        )
        self.assertIn(resize, calls)
        self.assertEqual(self.order, ["passphrase", {"passphrase": b"p" * 64}])
        self.assertEqual([c for c in self.limactl if c[:2] != ["limactl", "list"]], [])

    def test_a_finished_grow_that_has_nothing_left_to_grow_is_not_an_error(self):
        # The previous run grew the root and failed afterwards: asking for the
        # same ceiling again changes nothing, and that is the right answer.
        same = self.UNGROWN
        self.disk_grow(size="100", df_k=same)
        self.assertTrue(any("nothing left to grow" in line for line in self.out))

    def test_a_finished_grow_whose_partition_never_grew_is_not_nothing_left(self):
        # growpart failed at the previous boot: the ceiling is edited, vda2 is
        # still the old size, and the mapping and filesystem have nothing to
        # grow into, so df cannot tell this from a grow that is already done.
        same = self.UNGROWN
        with self.assertRaisesRegex(yo.YoError, "growpart did not grow it.*growpart"):
            self.disk_grow(size="100", df_k=same, vda2_bytes=50 * 1024**3)
        self.assertFalse(any("nothing left to grow" in line for line in self.out))

    def test_an_unreadable_partition_size_is_not_nothing_left_to_grow(self):
        same = self.UNGROWN
        with self.assertRaisesRegex(yo.YoError, "cannot read the size of /dev/vda2"):
            self.disk_grow(size="100", df_k=same, vda2_bytes="")

    def test_the_grown_partition_size_is_reported(self):
        self.disk_grow()
        self.assertIn("after:  vda2 is 199.8G", self.out)

    def test_a_partition_size_that_cannot_be_read_afterwards_says_why_and_the_grow_stands(
        self,
    ):
        self.vda2_listing = (1, "", "lsblk: /dev/vda2: not a block device\n")
        self.disk_grow()
        report = [line for line in self.out if line.startswith("after:  vda2")]
        self.assertEqual(len(report), 1)
        self.assertIn("exit 1", report[0])
        self.assertIn("not a block device", report[0])

    def test_a_new_ceiling_that_grows_nothing_is_an_error(self):
        same = self.UNGROWN
        with self.assertRaisesRegex(yo.YoError, "did not grow"):
            self.disk_grow(size="200", df_k=same)

    def test_an_unreadable_df_stops_before_the_ceiling_is_edited(self):
        with self.assertRaisesRegex(
            yo.YoError, "cannot read the VM's root usage from df.* — is it up"
        ):
            self.disk_grow(size="200", df_k=("", ""))
        self.assertNotIn("stop", self.order)
        self.assertEqual([c for c in self.limactl if c[:2] != ["limactl", "list"]], [])

    def test_a_df_that_cannot_be_read_afterwards_leaves_the_grow_unverified(self):
        with self.assertRaisesRegex(yo.YoError, "grow is unverified"):
            self.disk_grow(size="200", df_k=(self.GROWN[0], "garbage"))

    def test_a_ceiling_below_the_current_one_is_refused_before_anything_runs(self):
        with self.assertRaises(yo.YoError) as caught:
            self.disk_grow(size="50")
        self.assertIn("at least 100", caught.exception.message)
        self.assertEqual(self.order, [])

    def test_an_unencrypted_disk_is_refused_before_anything_stops(self):
        with self.assertRaises(yo.YoError) as caught:
            self.disk_grow(fstype="ext4\n")
        self.assertIn("yo bootstrap", caught.exception.message)
        self.assertEqual(self.order, [])


if __name__ == "__main__":
    unittest.main()
