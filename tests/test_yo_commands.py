import contextlib
import io
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_yo import FakeHome, Reached, yo


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
    # box that has drifted from this host's yo. If argparse ever ate --impure or
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
        args = self.parse(["ssh", "journalctl", "--user", "-u", "t3code", "-n", "50"])
        self.assertEqual(args.ssh_cmd, ["journalctl", "--user", "-u", "t3code", "-n", "50"])

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

        for name in ("vm_up", "seed_all", "vm_services", "ensure_herdr_machine", "ensure_guest_known_hosts"):
            patcher = mock.patch.object(yo, name)
            patcher.start()
            self.addCleanup(patcher.stop)

        ssh_run_patcher = mock.patch.object(
            yo, "ssh_run", return_value=mock.Mock(returncode=0, stdout="501", stderr="")
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

    def test_first_generation_on_a_fresh_box_restarts_without_reading_generations(self):
        with mock.patch.object(yo, "helper_missing", return_value=True):
            calls = self.run_bootstrap([("g1", "g1")])
        self.assertEqual(calls, [["limactl", "restart", "yolobox"]])

    def test_no_restart_when_already_running_the_built_generation(self):
        calls = self.run_bootstrap([("g1", "g1")])
        self.assertNotIn(["limactl", "restart", "yolobox"], calls)

    def test_restarts_exactly_once_when_the_second_read_agrees(self):
        calls = self.run_bootstrap([("g1", "g0"), ("g1", "g1")])
        self.assertEqual(calls, [["limactl", "restart", "yolobox"]])

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

    def test_seed_ssh_include_and_herdr_run_before_vm_services(self):
        # M1: an optional-service failure in vm_services must never skip
        # seeding identities, the ssh config include, or herdr registration —
        # so vm_services has to be the very last thing cmd_bootstrap does.
        order = []

        def record(name):
            def side_effect(*args, **kwargs):
                order.append(name)

            return side_effect

        for name in (
            "seed_all",
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
                "seed_all",
                "ensure_ssh_config_include",
                "ensure_guest_known_hosts",
                "ensure_herdr_machine",
                "vm_services",
            ],
        )

    def test_aborts_before_the_rebuild_when_the_operator_is_a_foreign_uid(self):
        rebuild_argv = []

        def fake_ssh_run(role, argv, **kwargs):
            if list(argv) == ["id", "-u"]:
                return mock.Mock(returncode=0, stdout="1000", stderr="")
            rebuild_argv.append(list(argv))
            return mock.Mock(returncode=0, stdout="", stderr="")

        args = yo.build_parser().parse_args(["bootstrap"])
        with mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run):
            with mock.patch.object(yo, "err") as fake_err:
                with self.assertRaises(yo.YoError):
                    yo.cmd_bootstrap(args)
        self.assertEqual(rebuild_argv, [])
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("limactl delete", messages)
        self.assertIn("yo bootstrap", messages)


class TestRequireOperatorUid(unittest.TestCase):
    def test_refuses_on_a_foreign_uid_naming_cause_and_remedy(self):
        with mock.patch.object(
            yo, "ssh_run", return_value=mock.Mock(returncode=0, stdout="1000\n", stderr="")
        ):
            with mock.patch.object(yo, "err") as fake_err:
                with self.assertRaises(yo.YoError):
                    yo.require_operator_uid()
        messages = " ".join(call.args[0] for call in fake_err.call_args_list)
        self.assertIn("user.uid", messages)
        self.assertIn("limactl delete yolobox && yo bootstrap", messages)

    def test_passes_on_the_pinned_uid(self):
        with mock.patch.object(
            yo, "ssh_run", return_value=mock.Mock(returncode=0, stdout="501\n", stderr="")
        ):
            yo.require_operator_uid()


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

    def test_skips_the_collection_when_the_generations_disagree(self):
        stdout = self.run_decision("/nix/store/aaa", "/nix/store/bbb")
        self.assertIn(
            "FAIL:skipped nix-collect-garbage -d: booted and profile generations disagree",
            stdout,
        )


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
                    yo.ensure_herdr_machine()
        fake_run.assert_not_called()
        fake_err.assert_called_once()

    def test_existing_machine_is_left_alone(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout='[{"target": "yolobox"}]', stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"):
            with mock.patch.object(yo, "run", side_effect=fake_run) as tracked:
                yo.ensure_herdr_machine()
        calls = [call.args[0] for call in tracked.call_args_list]
        self.assertTrue(all(argv[2:3] != ["add"] for argv in calls))

    def test_missing_machine_is_added(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"):
            with mock.patch.object(yo, "run", side_effect=fake_run):
                yo.ensure_herdr_machine()
        self.assertIn(
            ["/opt/homebrew/bin/herdr", "machine", "add", "yolobox", "--label", "yolobox"],
            calls,
        )

    def test_add_failure_raises_with_herdrs_stderr(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["machine", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(yo.shutil, "which", return_value="/opt/homebrew/bin/herdr"):
            with mock.patch.object(yo, "run", side_effect=fake_run):
                with self.assertRaises(yo.YoError) as caught:
                    yo.ensure_herdr_machine()
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
            with mock.patch.object(
                yo, "ensure_aws_broker", return_value=True
            ) as fake_aws, mock.patch.object(
                yo, "ensure_op_forward", return_value=[]
            ) as fake_forward, mock.patch.object(yo, "err") as fake_err:
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
        status_patcher = mock.patch.object(yo, "account_status", return_value=("agent", ""))
        status_patcher.start()
        self.addCleanup(status_patcher.stop)

        units_patcher = mock.patch.object(yo, "vm_units_present", return_value=True)
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_a_broken_aws_broker_does_not_stop_the_forward_healing(self):
        with mock.patch.object(
            yo, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
        ), mock.patch.object(yo, "ensure_op_forward", return_value=[]) as fake_forward:
            with self.assertRaises(yo.YoError):
                yo.vm_services()
        fake_forward.assert_called_once_with(False)

    def test_errors_from_both_phases_are_collected_and_raised_together(self):
        with mock.patch.object(
            yo, "ensure_aws_broker", side_effect=yo.YoError("broker exploded")
        ), mock.patch.object(yo, "ensure_op_forward", return_value=["1password forward dead"]):
            with self.assertRaises(yo.YoError) as caught:
                yo.vm_services()
        self.assertIn("broker exploded", caught.exception.message)
        self.assertIn("1password forward dead", caught.exception.message)

    def test_no_errors_from_either_phase_does_not_raise(self):
        with mock.patch.object(yo, "ensure_aws_broker", return_value=True), mock.patch.object(
            yo, "ensure_op_forward", return_value=[]
        ):
            yo.vm_services()


class TestHealReverseForward(FakeHome):
    def test_alive_probe_never_touches_the_forward(self):
        forward = yo.ReverseForward("x", "/guest", "/host", ("probe",), lambda proc: True)
        with mock.patch.object(yo, "ssh_run", return_value=mock.Mock(returncode=0)) as fake_ssh, mock.patch.object(
            yo, "run"
        ) as fake_run:
            self.assertTrue(yo.heal_reverse_forward(forward))
        fake_ssh.assert_called_once_with(yo.AGENT, ["probe"])
        fake_run.assert_not_called()

    def test_dead_probe_reissues_cancel_then_rm_then_forward(self):
        forward = yo.ReverseForward("x", "/guest", "/host", ("probe",), lambda proc: proc.returncode == 0)
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

        with mock.patch.object(yo, "run", side_effect=fake_run), mock.patch.object(
            yo, "ssh_run", side_effect=fake_ssh_run
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
        with mock.patch.object(yo.os.path, "exists", return_value=False), mock.patch.object(
            yo, "heal_reverse_forward", return_value=True
        ) as fake_heal, mock.patch.object(yo, "err") as fake_err:
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(errors, [])
        fake_heal.assert_called_once()
        fake_err.assert_called_once()

    def test_aws_disabled_never_attempts_its_forward(self):
        with mock.patch.object(yo.os.path, "exists", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", return_value=True
        ) as fake_heal:
            yo.ensure_op_forward(aws_enabled=False)
        self.assertEqual(fake_heal.call_count, 1)
        self.assertEqual(fake_heal.call_args[0][0].name, "1password")

    def test_both_forwards_failing_are_both_reported(self):
        with mock.patch.object(yo.os.path, "exists", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", return_value=False
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(len(errors), 2)

    def test_1password_forward_raising_still_heals_the_aws_forward(self):
        def fake_heal(forward):
            if forward.name == "1password":
                raise yo.YoError("1password forward exploded")
            return False

        with mock.patch.object(yo.os.path, "exists", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", side_effect=fake_heal
        ) as fake_heal_mock:
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(fake_heal_mock.call_count, 2)
        self.assertTrue(any("1password forward exploded" in e for e in errors))
        self.assertTrue(any("aws-broker" in e for e in errors))

    def test_aws_forward_raising_does_not_swallow_the_1password_result(self):
        def fake_heal(forward):
            if forward.name == "aws-broker":
                raise yo.YoError("aws-broker forward exploded")
            return True

        with mock.patch.object(yo.os.path, "exists", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", side_effect=fake_heal
        ):
            errors = yo.ensure_op_forward(aws_enabled=True)
        self.assertEqual(errors, ["the aws-broker forward into the VM failed while healing it: aws-broker forward exploded"])


class TestCmdAwsCheck(unittest.TestCase):
    def setUp(self):
        units_patcher = mock.patch.object(yo, "vm_units_present", return_value=True)
        units_patcher.start()
        self.addCleanup(units_patcher.stop)

    def test_units_missing_raises_naming_bootstrap_before_anything_else(self):
        with mock.patch.object(yo, "vm_units_present", return_value=False), mock.patch.object(
            yo, "ensure_aws_broker"
        ) as fake_aws:
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
        with mock.patch.object(yo, "ensure_aws_broker", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", side_effect=lambda fw: calls.append("heal") or True
        ), mock.patch.object(
            yo, "guest_helper", side_effect=lambda *a, **k: calls.append("guest") or mock.Mock(returncode=0)
        ):
            yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        self.assertEqual(calls, ["heal", "guest"])

    def test_dead_forward_raises_before_the_guest_check(self):
        with mock.patch.object(yo, "ensure_aws_broker", return_value=True), mock.patch.object(
            yo, "heal_reverse_forward", return_value=False
        ), mock.patch.object(yo, "guest_helper") as fake_guest:
            with self.assertRaises(yo.YoError):
                yo.cmd_aws_check(yo.build_parser().parse_args(["aws-check"]))
        fake_guest.assert_not_called()


class TestCmdStatus(unittest.TestCase):
    def run_status(self, *, units_present, gaps, aws_which, aws_allowlist_side_effect):
        outs = []

        def fake_out(line=""):
            outs.append(line)

        def fake_ssh_run(role, argv, **kwargs):
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            yo, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ), mock.patch.object(yo, "ssh_run", side_effect=fake_ssh_run), mock.patch.object(
            yo, "du_kib", return_value=None
        ), mock.patch.object(
            yo, "vm_units_present", return_value=units_present
        ), mock.patch.object(
            yo, "lima_config_gaps", return_value=gaps
        ), mock.patch.object(
            yo.shutil, "which", return_value=aws_which
        ), mock.patch.object(
            yo, "aws_allowlist", side_effect=aws_allowlist_side_effect
        ), mock.patch.object(
            yo, "out", side_effect=fake_out
        ), mock.patch.object(yo, "err"):
            yo.cmd_status(yo.build_parser().parse_args(["status"]))
        return outs

    def test_old_box_prints_units_missing_note_instead_of_proxy_and_1password_lines(self):
        outs = self.run_status(
            units_present=False, gaps=[], aws_which=None, aws_allowlist_side_effect=lambda: []
        )
        self.assertTrue(any(yo.UNITS_MISSING_NOTE in line for line in outs))
        self.assertFalse(any(line.startswith("1password:") for line in outs))
        self.assertFalse(any(line.startswith("proxy:") for line in outs))

    def test_units_present_prints_1password_and_proxy_lines(self):
        outs = self.run_status(
            units_present=True, gaps=[], aws_which=None, aws_allowlist_side_effect=lambda: []
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
        self.assertTrue(any(line.startswith("lima:") and yo.OP_GUEST_SOCK in line for line in outs))

    def test_no_lima_config_gap_is_reported_as_ok(self):
        outs = self.run_status(
            units_present=True, gaps=[], aws_which=None, aws_allowlist_side_effect=lambda: []
        )
        self.assertIn("lima: reverse forwards ok", outs)

    def test_malformed_aws_config_reports_unreadable_instead_of_raising(self):
        outs = self.run_status(
            units_present=True,
            gaps=[],
            aws_which="/usr/local/bin/aws",
            aws_allowlist_side_effect=yo.YoError("cannot parse ~/.aws/config: bad bytes"),
        )
        self.assertTrue(any("aws: config unreadable:" in line for line in outs))


if __name__ == "__main__":
    unittest.main()
