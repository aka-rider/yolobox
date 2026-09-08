import contextlib
import io
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_yo import FakeHome, Reached, yo


class TestForwardedEnvMatchesGuestAcceptEnv(unittest.TestCase):
    def test_every_forwarded_name_is_in_the_guest_whitelist(self):
        base_nix = Path(yo.__file__).parent / "nix" / "base.nix"
        text = base_nix.read_text()
        match = re.search(r"AcceptEnv\s*=\s*\[(.*?)\];", text, re.S)
        self.assertIsNotNone(match)
        names = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(names, set(yo.FORWARDED_ENV))


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
                "ssh", "sudo", "YOLOBOX_USERNAME=x", "nixos-rebuild",
                "switch", "--impure", "--flake", ".#yolobox",
            ]
        )
        self.assertEqual(
            args.ssh_cmd,
            [
                "sudo", "YOLOBOX_USERNAME=x", "nixos-rebuild",
                "switch", "--impure", "--flake", ".#yolobox",
            ],
        )

    def test_a_plain_remote_command_round_trips_intact(self):
        args = self.parse(["ssh", "journalctl", "-u", "t3", "-n", "50"])
        self.assertEqual(args.ssh_cmd, ["journalctl", "-u", "t3", "-n", "50"])

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

        for name in ("vm_up", "seed_all"):
            patcher = mock.patch.object(yo, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        ssh_run_patcher = mock.patch.object(
            yo, "ssh_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        )
        ssh_run_patcher.start()
        self.addCleanup(ssh_run_patcher.stop)

        include_patcher = mock.patch.object(yo, "ssh_config_include_line", return_value=1)
        include_patcher.start()
        self.addCleanup(include_patcher.stop)

        out_patcher = mock.patch.object(yo, "out")
        out_patcher.start()
        self.addCleanup(out_patcher.stop)

    def run_bootstrap(self, generations):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap"])
        with mock.patch.object(yo, "run", side_effect=fake_run):
            with mock.patch.object(yo, "guest_generations", side_effect=generations):
                yo.cmd_bootstrap(args)
        return calls

    def test_no_restart_when_already_running_the_built_generation(self):
        calls = self.run_bootstrap([("g1", "g1")])
        self.assertNotIn(["limactl", "restart", "yolobox"], calls)

    def test_restarts_exactly_once_when_the_second_read_agrees(self):
        calls = self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(calls, [["limactl", "restart", "yolobox"]])

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


class TestGcSkipsCollectionOnHalfFailedSwitch(unittest.TestCase):
    # Precedent: TestForwardedEnvMatchesGuestAcceptEnv above reads nix/base.nix
    # off disk to keep Python and Nix honest with each other. The guard this
    # exercises moved from yo's GC_MACHINE string into cmd_gc_machine in
    # nix/guest/yolobox-guest.sh, so the test now reads the guard from there.
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

    def test_skips_the_collection_when_the_generations_disagree(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/bbb")
        self.assertIn(
            "FAIL:skipped nix-collect-garbage -d: booted and profile generations disagree",
            stdout,
        )


class TestAwsRegion(unittest.TestCase):
    def test_reads_the_profiles_configured_region(self):
        with mock.patch.object(yo, "require_aws_cli"):
            with mock.patch.object(
                yo, "run", return_value=mock.Mock(returncode=0, stdout="eu-west-1\n", stderr="")
            ):
                self.assertEqual(yo.aws_region("p"), "eu-west-1")

    def test_falls_back_to_the_hosts_default_region(self):
        with mock.patch.object(yo, "require_aws_cli"):
            with mock.patch.object(
                yo, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                with env_without("AWS_REGION"):
                    with mock.patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1"}):
                        self.assertEqual(yo.aws_region("p"), "us-east-1")

    def test_refuses_when_no_region_is_configured_anywhere(self):
        with mock.patch.object(yo, "require_aws_cli"):
            with mock.patch.object(
                yo, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                with env_without("AWS_REGION", "AWS_DEFAULT_REGION"):
                    with self.assertRaises(yo.YoError) as caught:
                        yo.aws_region("p")
        self.assertIn("region", caught.exception.message)


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

    def test_outside_home_lands_on_the_agent_home(self):
        with mock.patch.object(yo, "logical_cwd", return_value="/etc"):
            with mock.patch.object(yo, "ssh_run") as fake_ssh_run:
                with mock.patch.object(yo, "err") as fake_err:
                    result = yo.dir_for_cwd()
        fake_ssh_run.assert_not_called()
        self.assertEqual(result, yo.AGENT_HOME)
        fake_err.assert_called_once_with(yo.MISSING_PATH_NOTE % ("/etc", yo.AGENT_HOME))


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
            with mock.patch.object(yo, "ssh_run", side_effect=self.fake_ssh_run(main_proc)):
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
            with mock.patch.object(yo, "ssh_run", side_effect=self.fake_ssh_run(main_proc)):
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
                return mock.Mock(returncode=0 if helper_present else 1, stdout="", stderr="")
            return main_proc

        return run

    def test_streamed_absent_helper_names_yo_bootstrap(self):
        main_proc = mock.Mock(returncode=127, stderr=None, stdout=None)
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo, "ssh_run", side_effect=self.streamed_ssh_run(main_proc, helper_present=False)
            ):
                with self.assertRaises(yo.YoError) as caught:
                    yo.guest_helper(yo.OPERATOR, "gc-machine", stream=True)
        self.assertIn("yo bootstrap", caught.exception.message)

    def test_streamed_unknown_subcommand_names_the_subcommand(self):
        main_proc = mock.Mock(returncode=64, stderr=None, stdout=None)
        with mock.patch.object(yo, "YO_VERSION", "1.2.3"):
            with mock.patch.object(
                yo, "ssh_run", side_effect=self.streamed_ssh_run(main_proc, helper_present=True)
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
    # ext4's root reserve is the operator's alone (see CLAUDE.md), so on a
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


if __name__ == "__main__":
    unittest.main()
