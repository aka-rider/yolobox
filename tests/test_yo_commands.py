import contextlib
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
            with mock.patch.object(yo, "out"):
                self.assertEqual(yo.main(["--version"]), 0)

    def test_help_flag_skips_the_limactl_preflight(self):
        with mock.patch.object(yo, "require_tool", side_effect=Reached):
            with mock.patch.object(yo, "out"):
                self.assertEqual(yo.main(["--help"]), 0)

    def test_known_command_runs_the_preflight_before_the_command(self):
        def raise_reached(args):
            raise Reached()

        with mock.patch.object(
            yo, "require_tool", side_effect=yo.YoError("limactl not found")
        ):
            with mock.patch.dict(yo.COMMANDS, {"status": raise_reached}):
                with self.assertRaises(yo.YoError):
                    yo.main(["status"])

    def test_no_arguments_is_a_usage_error(self):
        with mock.patch.object(yo, "err"):
            self.assertEqual(yo.main([]), 2)

    def test_unknown_command_is_a_usage_error(self):
        with mock.patch.object(yo, "err"):
            self.assertEqual(yo.main(["bogus"]), 2)


class TestBootstrapRebootDecision(unittest.TestCase):
    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {"YOLOBOX_FLAKE": "github:x/y"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        for name in ("cmd_up", "cmd_seed"):
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

        with mock.patch.object(yo, "run", side_effect=fake_run):
            with mock.patch.object(yo, "guest_generations", side_effect=generations):
                yo.cmd_bootstrap([])
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
    def run_decision(self, booted, profile):
        match = re.search(
            r'booted="\$\(readlink -f /run/current-system\)".*?\nfi\n',
            yo.GC_MACHINE,
            re.S,
        )
        self.assertIsNotNone(match)
        fragment = match.group(0)
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
        ) + fragment
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
        with mock.patch.object(yo, "logical_cwd", return_value=desired):
            with mock.patch.object(yo, "agent_out", return_value=landing + "\n"):
                with mock.patch.object(yo, "err") as fake_err:
                    result = yo.dir_for_cwd()
        self.assertEqual(result, landing)
        fake_err.assert_called_once_with(
            yo.MISSING_PATH_NOTE % (yo.AGENT_HOME + "/a/b", landing)
        )

    def test_outside_home_lands_on_the_agent_home(self):
        with mock.patch.object(yo, "logical_cwd", return_value="/etc"):
            with mock.patch.object(yo, "agent_out") as fake_agent_out:
                with mock.patch.object(yo, "err") as fake_err:
                    result = yo.dir_for_cwd()
        fake_agent_out.assert_not_called()
        self.assertEqual(result, yo.AGENT_HOME)
        fake_err.assert_called_once_with(yo.MISSING_PATH_NOTE % ("/etc", yo.AGENT_HOME))


if __name__ == "__main__":
    unittest.main()
