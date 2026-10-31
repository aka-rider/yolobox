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
from pathlib import Path
from unittest import mock

from test_yo import FIXTURES, FakeHome, Reached, yo


@contextlib.contextmanager
def env_without(*names):
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in names:
            os.environ.pop(name, None)
        yield


class TestFlakeRef(unittest.TestCase):
    def test_override_wins_verbatim(self):
        with mock.patch.dict(os.environ, {"YOLOBOX_FLAKE": "github:x/y"}):
            self.assertEqual(yo.flake_ref(), "github:x/y")

    def test_dev_build_refuses_with_no_pinned_flake(self):
        with env_without("YOLOBOX_FLAKE"), mock.patch.object(yo, "YO_VERSION", "dev"):
            with self.assertRaises(yo.YoError) as caught:
                yo.flake_ref()
        self.assertIn("YOLOBOX_FLAKE", caught.exception.message)

    def test_released_version_pins_the_tagged_ref(self):
        with env_without("YOLOBOX_FLAKE"), mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            self.assertEqual(yo.flake_ref(), "github:aka-rider/yolobox/v1.2.3")

    def test_the_hint_quotes_a_ref_with_shell_metacharacters(self):
        ref = "github:x/y'; touch pwned #"
        with mock.patch.dict(os.environ, {"YOLOBOX_FLAKE": ref}):
            hint = yo.rebuild_hint()
        flake_arg = hint.split("--flake ", 1)[1]
        self.assertEqual(shlex.split(flake_arg), [ref + "#yolobox"])

    def test_dev_build_falls_back_to_a_hint_naming_the_override(self):
        with env_without("YOLOBOX_FLAKE"), mock.patch.object(yo, "YO_VERSION", "dev"):
            with mock.patch.object(yo, "err") as fake_err:
                hint = yo.rebuild_hint()
        self.assertIn("YOLOBOX_FLAKE", hint)
        fake_err.assert_called_once()
        self.assertIn("YOLOBOX_FLAKE", fake_err.call_args[0][0])


class TestMainPreflight(unittest.TestCase):
    def test_version_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_help_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo, "require_tool", side_effect=Reached):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    yo.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_known_command_runs_the_preflight_before_the_command(self):
        def raise_reached(args):
            raise Reached()

        with mock.patch.object(
            yo, "require_tool", side_effect=yo.YoError("limactl not found")
        ):
            with mock.patch.object(yo, "cmd_status", side_effect=raise_reached):
                with self.assertRaises(yo.YoError):
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


class TestSshRemainderPassthrough(unittest.TestCase):
    # rebuild_hint() prints "yo ssh sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild
    # switch --impure --flake '<ref>#yolobox'" as the documented remedy for a
    # box that has drifted from this Mac's yo. If argparse ever ate --impure or
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
        with mock.patch.object(yo, "pick_project", return_value="picked") as fake_pick:
            with mock.patch.object(yo, "dir_for_cwd") as fake_cwd:
                self.assertEqual(yo.target_dir(""), "picked")
        fake_pick.assert_called_once_with("")
        fake_cwd.assert_not_called()

    def test_target_dir_falls_back_to_the_cwd_when_no_project_was_given(self):
        with mock.patch.object(yo, "pick_project") as fake_pick:
            with mock.patch.object(yo, "dir_for_cwd", return_value="cwd") as fake_cwd:
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


class TestBootstrapRebootDecision(unittest.TestCase):
    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {"YOLOBOX_FLAKE": "github:x/y"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        for name in (
            "vm_up",
            "vm_services",
            "ensure_herdr_machine",
            "ensure_guest_known_hosts",
        ):
            patcher = mock.patch.object(yo, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        fstype_patcher = mock.patch.object(
            yo, "luks_device_fstype", return_value="crypto_LUKS"
        )
        fstype_patcher.start()
        self.addCleanup(fstype_patcher.stop)

        ssh_run_patcher = mock.patch.object(
            yo, "ssh_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        )
        ssh_run_patcher.start()
        self.addCleanup(ssh_run_patcher.stop)

        include_patcher = mock.patch.object(
            yo, "ssh_config_include_line", return_value=1
        )
        include_patcher.start()
        self.addCleanup(include_patcher.stop)

        out_patcher = mock.patch.object(yo, "out")
        out_patcher.start()
        self.addCleanup(out_patcher.stop)

        err_patcher = mock.patch.object(yo, "err")
        err_patcher.start()
        self.addCleanup(err_patcher.stop)

    def run_bootstrap_with_real_generations(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap"])
        with mock.patch.object(yo, "run", side_effect=fake_run):
            yo.cmd_bootstrap(args)
        return calls

    def run_bootstrap(self, generations):
        with mock.patch.object(yo, "guest_generations", side_effect=generations):
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

    def test_stops_exactly_once_then_starts_again_when_the_second_read_agrees(self):
        calls = self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(calls, [["limactl", "stop", "yolobox"]])
        self.assertEqual(
            [call.kwargs for call in yo.vm_up.call_args_list],
            [
                {"run_services": False},
                {"run_services": False, "passphrase": yo.vm_up.return_value},
            ],
        )

    def test_the_passphrase_is_derived_before_the_vm_stops(self):
        # A missing 1Password key must leave the running box alone.
        yo.vm_up.return_value = None
        order = []
        with (
            mock.patch.object(
                yo,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"a" * 64,
            ),
            mock.patch.object(
                yo, "lima_stop", side_effect=lambda: order.append("stop")
            ),
        ):
            self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(order, ["passphrase", "stop"])

    def test_vm_up_brings_the_vm_up_without_services(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_up.assert_called_once_with(run_services=False)

    def test_vm_services_runs_once_after_deciding_the_generation_already_matches(self):
        self.run_bootstrap([("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_runs_once_after_a_reboot_settles_the_generation(self):
        self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        yo.vm_services.assert_called_once_with()

    def test_vm_services_does_not_run_when_the_generation_never_settles(self):
        with mock.patch.object(yo, "err"):
            with self.assertRaises(yo.YoError):
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
        yo.vm_services.assert_not_called()

    def test_aborts_naming_boot_partition_when_generations_still_disagree(self):
        with mock.patch.object(yo, "err") as fake_err:
            with self.assertRaises(yo.YoError):
                self.run_bootstrap([("g1", "g0"), ("g1", "g2")])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("df /boot", messages)

    def test_aborts_naming_yo_status_when_booted_is_unreadable(self):
        with mock.patch.object(yo, "err") as fake_err:
            with self.assertRaises(yo.YoError):
                self.run_bootstrap([("g1", "")])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("yo status", messages)

    def test_failed_stop_aborts_naming_the_recovery(self):
        def fake_run(argv, **kwargs):
            if argv[:2] == ["limactl", "stop"]:
                raise yo.YoError("limactl stop yolobox failed (exit 1)")
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap"])
        with (
            mock.patch.object(yo, "err") as fake_err,
            mock.patch.object(yo, "run", side_effect=fake_run),
            mock.patch.object(yo, "guest_generations", return_value=("g1", "g0")),
        ):
            with self.assertRaises(yo.YoError):
                yo.cmd_bootstrap(args)
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("limactl stop failed", messages)
        self.assertIn("yo bootstrap", messages)

    def test_herdr_is_told_whether_the_ssh_include_is_in_place(self):
        for included in (True, False):
            with self.subTest(included=included):
                with mock.patch.object(
                    yo, "ensure_ssh_config_include", return_value=included
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

        for name in (
            "ensure_ssh_config_include",
            "ensure_guest_known_hosts",
            "ensure_herdr_machine",
            "vm_services",
        ):
            patcher = mock.patch.object(yo, name, side_effect=record(name))
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


class TestBootstrapWarnsBeforeEncryptingInPlace(unittest.TestCase):
    def run_bootstrap(self, fstype, df_line):
        rebuilds = self.rebuilds = []

        def fake_ssh_run(role, argv, **kwargs):
            if argv[:2] == ["lsblk", "-dno"]:
                return mock.Mock(returncode=0, stdout=fstype + "\n", stderr="")
            if argv[:2] == ["df", "-P"]:
                header = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                return mock.Mock(returncode=0, stdout=header + df_line, stderr="")
            if "nixos-rebuild" in argv:
                rebuilds.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap"])
        with (
            mock.patch.dict(os.environ, {"YOLOBOX_FLAKE": "github:x/y"}),
            mock.patch.object(yo, "vm_up"),
            mock.patch.object(yo, "vm_services"),
            mock.patch.object(yo, "ensure_herdr_machine"),
            mock.patch.object(yo, "ensure_guest_known_hosts"),
            mock.patch.object(yo, "ensure_ssh_config_include"),
            mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(yo, "guest_generations", return_value=("g1", "g1")),
            mock.patch.object(yo, "out"),
            mock.patch.object(yo, "err") as fake_err,
        ):
            try:
                yo.cmd_bootstrap(args)
            finally:
                self.stderr = " ".join(call.args[0] for call in fake_err.call_args_list)
        return rebuilds

    def test_an_unencrypted_disk_gets_the_no_interruption_warning(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 500 500 50% /\n")
        self.assertEqual(len(rebuilds), 1)
        self.assertIn("encrypts /dev/vda2 in place", self.stderr)
        self.assertIn("must not be interrupted", self.stderr)

    def test_an_encrypted_disk_gets_no_warning(self):
        rebuilds = self.run_bootstrap(
            "crypto_LUKS", "/dev/mapper/root 1000 990 10 99% /\n"
        )
        self.assertEqual(len(rebuilds), 1)
        self.assertNotIn("in place", self.stderr)

    def test_a_disk_over_90_percent_full_is_refused(self):
        with self.assertRaises(yo.YoError) as caught:
            self.run_bootstrap("ext4", "/dev/vda2 1000 950 50 95% /\n")
        self.assertIn("95% full", caught.exception.message)
        self.assertIn("yo gc --deep --yes", caught.exception.message)
        self.assertEqual(self.rebuilds, [])

    def test_exactly_90_percent_full_is_still_allowed(self):
        rebuilds = self.run_bootstrap("ext4", "/dev/vda2 1000 900 100 90% /\n")
        self.assertEqual(len(rebuilds), 1)

    def test_an_unreadable_df_is_an_error_not_a_pass(self):
        with self.assertRaises(yo.YoError):
            self.run_bootstrap("ext4", "garbage\n")

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
        guest_script = Path(yo.__file__).parent / "nix" / "guest" / "yolobox-guest.sh"
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
        with mock.patch.object(yo, "logical_cwd", return_value=desired):
            with mock.patch.object(yo, "ssh_run", return_value=proc) as fake_ssh_run:
                with mock.patch.object(yo, "err") as fake_err:
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
        with mock.patch.object(yo, "logical_cwd", return_value=self.home + "/a"):
            with mock.patch.object(yo, "ssh_run", return_value=proc):
                with mock.patch.object(yo, "err") as fake_err:
                    with self.assertRaises(yo.YoError):
                        yo.dir_for_cwd()
        fake_err.assert_not_called()

    def test_outside_home_lands_on_the_agent_home(self):
        with mock.patch.object(yo, "logical_cwd", return_value="/etc"):
            with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
                with mock.patch.object(yo, "err") as fake_err:
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
            mock.patch.object(yo, "ensure_ssh_config_include"),
            mock.patch.object(yo, "require_agent_account"),
            mock.patch.object(
                yo, "target_dir", return_value=yo.AGENT_HOME + "/a b/c#d?e%f"
            ),
            mock.patch.object(yo, "exec_process", side_effect=argv.extend),
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
        path = "/home/agent/wrk/rune"
        self.assertEqual(yo.editor_uri_path(path), path)


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
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo, "ssh_run", side_effect=self.fake_ssh_run(main_proc)
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
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo, "ssh_run", side_effect=self.fake_ssh_run(main_proc)
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.AGENT, "projects")
        self.assertIn("projects", caught.exception.message)

    def test_a_childs_own_exit_64_with_no_marker_is_not_reinterpreted(self):
        main_proc = mock.Mock(returncode=64, stderr="", stdout="")
        with mock.patch.object(yo, "ssh_run", return_value=main_proc):
            result = yo.guest_helper(yo.AGENT, "projects")
        self.assertIs(result, main_proc)

    def test_a_childs_own_exit_127_with_unrelated_stderr_is_not_reinterpreted(self):
        main_proc = mock.Mock(returncode=127, stderr="podman: exec failed\n", stdout="")
        with mock.patch.object(yo, "ssh_run", return_value=main_proc):
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
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo,
                "ssh_run",
                side_effect=self.streamed_ssh_run(main_proc, helper_present=False),
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIn("yo bootstrap", caught.exception.message)

    def test_streamed_unknown_subcommand_names_the_subcommand(self):
        main_proc = mock.Mock(returncode=64, stderr=None, stdout=None)
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo,
                "ssh_run",
                side_effect=self.streamed_ssh_run(main_proc, helper_present=True),
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIn("gc-machine", caught.exception.message)

    def test_streamed_tier_failure_is_not_reinterpreted_as_skew(self):
        main_proc = mock.Mock(returncode=1, stderr=None, stdout=None)
        with mock.patch.object(yo, "ssh_run", return_value=main_proc):
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
        with mock.patch.object(yo, "guest_helper", side_effect=fake_guest_helper):
            with mock.patch.object(yo, "require_agent_account"):
                yo.cmd_gc(args)

        self.assertEqual(
            calls,
            [
                (yo.OPERATOR, "gc-machine", ["--apply"]),
                (yo.AGENT, "gc-user", ["--apply", "--deep"]),
            ],
        )


class TestCmdEnterArgv(FakeHome):
    def enter(self):
        captured = {}

        def fake_exec(argv, env=None):
            captured["argv"] = list(argv)

        with mock.patch.object(yo, "require_agent_account"):
            with mock.patch.object(yo, "target_dir", return_value=yo.AGENT_HOME):
                with mock.patch.object(yo, "exec_process", side_effect=fake_exec):
                    args = yo.build_parser().parse_args(["enter"])
                    yo.cmd_enter(args)
        return captured["argv"]

    def test_argv_carries_no_forward_agent(self):
        with env_without("HERDR_ENV"):
            argv = self.enter()
        self.assertNotIn("ForwardAgent", " ".join(argv))

    def test_herdr_env_prints_the_invisibility_notice(self):
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with mock.patch.object(yo, "err") as fake_err:
                self.enter()
        fake_err.assert_called_once()
        message = fake_err.call_args[0][0]
        self.assertIn("yolobox herdr machine", message)
        self.assertIn("invisible to herdr", message)

    def test_no_herdr_env_prints_nothing(self):
        with env_without("HERDR_ENV"):
            with mock.patch.object(yo, "err") as fake_err:
                self.enter()
        fake_err.assert_not_called()


class TestEnsureHerdrMachine(unittest.TestCase):
    def test_no_herdr_on_path_skips_with_a_note(self):
        with mock.patch.object(yo.shutil, "which", return_value=None):
            with mock.patch.object(yo, "run") as fake_run:
                with mock.patch.object(yo, "err") as fake_err:
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
            with mock.patch.object(yo, "run", side_effect=fake_run) as tracked:
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
            with mock.patch.object(yo, "run", side_effect=fake_run):
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
            with mock.patch.object(yo, "run", side_effect=fake_run):
                with mock.patch.object(yo, "err") as fake_err:
                    yo.ensure_herdr_machine(False)
        notes = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("~/.ssh/config does not include", notes)
        self.assertIn(yo.INCLUDE_LINE, notes)

    def test_add_failure_raises_with_herdrs_stderr(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(
            yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"
        ):
            with mock.patch.object(yo, "run", side_effect=fake_run):
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

        with mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run):
            with (
                mock.patch.object(
                    yo, "ensure_aws_broker", return_value=True
                ) as fake_aws,
                mock.patch.object(
                    yo, "ensure_op_forward", return_value=[]
                ) as fake_forward,
                mock.patch.object(yo, "err") as fake_err,
            ):
                yo.vm_services()
        return fake_aws, fake_forward, fake_err

    def test_operator_unreachable_skips_without_raising(self):
        fake_aws, fake_forward, fake_err = self.run_vm_services(
            agent_ok=False, operator_ok=False, units_ok=False
        )
        fake_aws.assert_not_called()
        fake_forward.assert_not_called()
        fake_err.assert_called_once()

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
            yo, "account_status", return_value=("agent", "")
        )
        status_patcher.start()
        self.addCleanup(status_patcher.stop)

        units_patcher = mock.patch.object(yo, "vm_units_present", return_value=True)
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_a_broken_aws_broker_does_not_stop_the_forward_healing(self):
        with (
            mock.patch.object(
                yo, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
            ),
            mock.patch.object(yo, "ensure_op_forward", return_value=[]) as fake_forward,
        ):
            with self.assertRaises(yo.YoError):
                yo.vm_services()
        fake_forward.assert_called_once_with(False)

    def test_errors_from_both_phases_are_collected_and_raised_together(self):
        with (
            mock.patch.object(
                yo, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
            ),
            mock.patch.object(
                yo, "ensure_op_forward", return_value=["1password forward dead"]
            ),
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_services()
        self.assertIn("broker exploded", caught.exception.message)
        self.assertIn("1password forward dead", caught.exception.message)

    def test_no_errors_from_either_phase_does_not_raise(self):
        with (
            mock.patch.object(yo, "ensure_aws_broker", return_value=True),
            mock.patch.object(yo, "ensure_op_forward", return_value=[]),
        ):
            yo.vm_services()


class TestHealReverseForward(FakeHome):
    def test_alive_probe_never_touches_the_forward(self):
        forward = yo.ReverseForward(
            "x", "/guest", "/host", ("probe",), lambda proc: True
        )
        with (
            mock.patch.object(
                yo, "ssh_run", return_value=mock.Mock(returncode=0)
            ) as fake_ssh,
            mock.patch.object(yo, "run") as fake_run,
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
            mock.patch.object(yo, "run", side_effect=fake_run),
            mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run),
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
                yo, "heal_reverse_forward", return_value=True
            ) as fake_heal,
            mock.patch.object(yo, "err") as fake_err,
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(errors, [])
        fake_heal.assert_called_once()
        fake_err.assert_called_once()

    def test_aws_disabled_never_attempts_its_forward(self):
        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(
                yo, "heal_reverse_forward", return_value=True
            ) as fake_heal,
        ):
            yo.ensure_op_forward(aws_enabled=False)
        self.assertEqual(fake_heal.call_count, 1)
        self.assertEqual(fake_heal.call_args[0][0].name, "1password")

    def test_both_forwards_failing_are_both_reported(self):
        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(yo, "heal_reverse_forward", return_value=False),
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(len(errors), 2)

    def test_1password_forward_raising_still_heals_the_aws_forward(self):
        def fake_heal(forward):
            if forward.name == "1password":
                raise yo.YoError("1password forward exploded")
            return False

        with (
            mock.patch.object(yo.os.path, "exists", return_value=True),
            mock.patch.object(
                yo, "heal_reverse_forward", side_effect=fake_heal
            ) as fake_heal_mock,
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
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
            mock.patch.object(yo, "heal_reverse_forward", side_effect=fake_heal),
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(
            errors,
            [
                "the aws-broker forward into the VM failed while healing it: aws-broker forward exploded"
            ],
        )


class TestCmdAwsCheck(unittest.TestCase):
    def setUp(self):
        units_patcher = mock.patch.object(yo, "vm_units_present", return_value=True)
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_units_missing_raises_naming_bootstrap_before_anything_else(self):
        with (
            mock.patch.object(yo, "vm_units_present", return_value=False),
            mock.patch.object(yo, "ensure_aws_broker") as fake_aws,
        ):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertEqual(caught.exception.message, yo.UNITS_MISSING_NOTE)
        fake_aws.assert_not_called()

    def test_disabled_raises_naming_why(self):
        with mock.patch.object(yo, "ensure_aws_broker", return_value=False):
            with self.assertRaises(yo.YoError) as caught:
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertIn("disabled", caught.exception.message)

    def test_forward_heal_runs_before_the_guest_check(self):
        calls = []
        with (
            mock.patch.object(yo, "ensure_aws_broker", return_value=True),
            mock.patch.object(
                yo,
                "heal_reverse_forward",
                side_effect=lambda fw: calls.append("heal") or True,
            ),
            mock.patch.object(
                yo,
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
            mock.patch.object(yo, "ensure_aws_broker", return_value=True),
            mock.patch.object(yo, "heal_reverse_forward", return_value=False),
            mock.patch.object(yo, "guest_helper") as fake_guest,
        ):
            with self.assertRaises(yo.YoError):
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        fake_guest.assert_not_called()


class TestCmdStatus(unittest.TestCase):
    def run_status(
        self,
        *,
        units_present,
        gaps,
        aws_which,
        aws_allowlist_side_effect,
        net_check=None,
        fstype="",
        egress_probe="LoadState=loaded\nActiveState=active\n",
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

        def fake_out(line=""):
            outs.append(line)

        def fake_ssh_run(role, argv, **kwargs):
            stdout = ""
            if argv[:2] == ["lsblk", "-dno"]:
                stdout = fstype
            elif argv == yo.EGRESS_PROBE:
                stdout = egress_probe
            elif argv == yo.EGRESS_TABLE_PROBE:
                stdout = (FIXTURES / "egress.nft").read_text()
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with (
            mock.patch.object(
                yo, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ),
            mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(yo, "du_kib", return_value=None),
            mock.patch.object(yo, "vm_units_present", return_value=units_present),
            mock.patch.object(yo, "lima_config_gaps", return_value=gaps),
            mock.patch.object(yo.shutil, "which", return_value=aws_which),
            mock.patch.object(
                yo, "aws_allowlist", side_effect=aws_allowlist_side_effect
            ),
            mock.patch.object(yo, "out", side_effect=fake_out),
            mock.patch.object(yo, "err"),
            mock.patch.object(yo, "guest_helper", helper),
        ):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))
        return outs

    def test_old_box_prints_units_missing_note_instead_of_proxy_and_1password_lines(
        self,
    ):
        outs = self.run_status(
            units_present=False,
            gaps=[],
            aws_which=None,
            aws_allowlist_side_effect=lambda: [],
        )
        self.assertTrue(any(yo.UNITS_MISSING_NOTE in line for line in outs))
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

    def test_lima_config_gap_is_reported_without_raising(self):
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

    def test_a_box_waiting_in_its_initrd_says_it_is_locked(self):
        outs = []
        with (
            mock.patch.object(yo, "run", return_value=mock.Mock(returncode=0)),
            mock.patch.object(yo, "account_status", return_value=("unreachable", "")),
            mock.patch.object(yo, "initrd_waiting", return_value=True) as waiting,
            mock.patch.object(yo, "lima_config_gaps", return_value=[]),
            mock.patch.object(yo, "du_kib", return_value=None),
            mock.patch.object(yo.shutil, "which", return_value=None),
            mock.patch.object(yo, "out", side_effect=outs.append),
            mock.patch.object(yo, "err"),
        ):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))
        waiting.assert_called_once_with()
        self.assertTrue(
            any(line.startswith("disk: locked") and "yo up" in line for line in outs)
        )

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
        self.assertIn("WARNING recovery mode", lines[0])
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
        self.assertIn(yo.network_status_line(yo.NetCheck(True, yo.Gateway.DEAD)), outs)

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


class TestGuestNetworkLine(unittest.TestCase):
    def test_net_check_runs_as_the_operator_with_no_arguments(self):
        helper = mock.Mock(
            return_value=mock.Mock(returncode=0, stdout="egress=ok\ngateway=ok\n")
        )
        with mock.patch.object(yo, "guest_helper", helper):
            line = yo.guest_network_line()
        helper.assert_called_once_with(yo.OPERATOR, "net-check", [])
        self.assertEqual(line, "network: ok")

    def test_a_box_that_still_wants_a_port_reports_not_checked(self):
        helper = mock.Mock(
            return_value=mock.Mock(returncode=2, stdout="", stderr="usage: net-check")
        )
        with mock.patch.object(yo, "guest_helper", helper):
            line = yo.guest_network_line()
        self.assertTrue(line.startswith("network: not checked — "))


class TestNetworkStatusLine(unittest.TestCase):
    def test_both_ok(self):
        self.assertEqual(
            yo.network_status_line(yo.NetCheck(True, yo.Gateway.OK)), "network: ok"
        )

    def test_dead_egress_with_answering_gateway_blames_a_default_route_vpn(self):
        line = yo.network_status_line(yo.NetCheck(False, yo.Gateway.OK))
        self.assertEqual(
            line,
            "network: WARNING guest egress is dead while lima's gateway answers DNS — "
            "a VPN on the host likely owns its default route",
        )

    def test_a_silent_gateway_with_working_egress_blames_the_hosts_vpn_or_dns_filter(
        self,
    ):
        line = yo.network_status_line(yo.NetCheck(True, yo.Gateway.DEAD))
        self.assertEqual(
            line,
            "network: WARNING lima's gateway does not answer DNS while egress works — "
            "DNS through the gateway will fail; check the host's VPN or DNS filter",
        )

    def test_both_dead_points_at_the_lima_line(self):
        line = yo.network_status_line(yo.NetCheck(False, yo.Gateway.DEAD))
        self.assertEqual(
            line,
            "network: WARNING the guest reaches neither the internet nor lima's gateway DNS — "
            "check yo status's lima line and the host's VPN",
        )

    def test_a_blocked_gateway_with_working_egress_says_the_probe_was_skipped(self):
        self.assertEqual(
            yo.network_status_line(yo.NetCheck(True, yo.Gateway.SKIPPED)),
            "network: ok (lima's gateway probe skipped: this box blocks it by design)",
        )

    def test_a_blocked_gateway_with_dead_egress_warns_without_blaming_a_vpn(
        self,
    ):
        self.assertEqual(
            yo.network_status_line(yo.NetCheck(False, yo.Gateway.SKIPPED)),
            "network: WARNING guest egress is dead "
            "(lima's gateway probe skipped: this box blocks it by design)",
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
            mock.patch.object(yo, "run", self.run),
            mock.patch.object(yo.subprocess, "Popen", self.popen),
            mock.patch.object(yo, "luks_passphrase", self.luks_passphrase),
            mock.patch.object(yo, "vm_services", self.vm_services),
            mock.patch.object(yo, "ensure_agent_ssh_config"),
            mock.patch.object(yo, "ensure_guest_known_hosts"),
            mock.patch.object(yo, "lima_config_gaps", return_value=[]),
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
        yo.cmd_up(yo.build_parser().parse_args(["up"]))


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
        self.assertIn("-p %d root@127.0.0.1" % yo.ssh_port(), message)

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


class TestLimaStop(unittest.TestCase):
    def test_a_hung_stop_carries_the_recovery(self):
        expired = subprocess.TimeoutExpired(["limactl", "stop"], yo.LIMA_STOP_TIMEOUT)
        with mock.patch.object(yo.subprocess, "run", side_effect=expired):
            with self.assertRaises(yo.YoError) as caught:
                yo.lima_stop()
        self.assertIn(
            "did not finish within %d s" % yo.LIMA_STOP_TIMEOUT,
            caught.exception.message,
        )
        self.assertIn("kill -9", caught.exception.message)
        self.assertIn("systemctl reboot", caught.exception.message)

    def test_a_stop_that_failed_for_another_reason_is_not_called_a_hang(self):
        failed = subprocess.CompletedProcess(["limactl", "stop"], 1, None, None)
        with mock.patch.object(yo.subprocess, "run", return_value=failed):
            with self.assertRaises(yo.YoError) as caught:
                yo.lima_stop()
        self.assertIn("failed", caught.exception.message)
        self.assertNotIn("kill -9", caught.exception.message)

    def test_down_stops_the_instance_under_the_timeout(self):
        with mock.patch.object(yo, "run") as fake_run:
            yo.cmd_down(yo.build_parser().parse_args(["down"]))
        fake_run.assert_called_once_with(
            ["limactl", "stop", "yolobox"],
            capture=False,
            check=True,
            timeout=yo.LIMA_STOP_TIMEOUT,
        )


class TestDiskGrowResizesTheMapping(FakeHome):
    def disk_grow(self, fstype="crypto_LUKS\n", size="200"):
        os.makedirs(os.path.join(self.home, ".lima"))
        calls = []
        df_k = iter(
            [
                "Filesystem 1K-blocks\n/dev/mapper/root 100\n",
                "x\n/dev/mapper/root 200\n",
            ]
        )

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
            elif argv[:3] == ["df", "-k", "/"]:
                stdout = next(df_k)
            elif argv[:2] == ["df", "-h"]:
                stdout = "Filesystem Size\n/dev/mapper/root 100G\n"
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        order = []
        with (
            mock.patch.object(yo, "run", side_effect=fake_run),
            mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run),
            mock.patch.object(
                yo, "lima_stop", side_effect=lambda: order.append("stop")
            ),
            mock.patch.object(
                yo, "vm_up", side_effect=lambda **kw: order.append(kw)
            ) as up,
            mock.patch.object(
                yo,
                "luks_passphrase",
                side_effect=lambda: order.append("passphrase") or b"p" * 64,
            ),
            mock.patch.object(yo, "out"),
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
