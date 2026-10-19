"""Behaviour of nix/guest/yolobox-guest.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options, and PATH holds a scratch directory with a
fake `curl` ahead of the system tools. Only subcommands that need nothing but
what macOS and Linux share are exercised: the others (`projects`,
`home-roots`, `gc-*`) use GNU find's `-printf`, sudo or podman.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

GUEST_SCRIPT = (
    Path(__file__).resolve().parent.parent / "nix" / "guest" / "yolobox-guest.sh"
)

FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CURL_ARGV"
printf '%s' "$FAKE_CURL_BODY"
exit "$FAKE_CURL_RC"
"""

BROKER_SOCK = "/run/yolobox/aws-broker.sock"


class GuestScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.curl_argv = self.tmp / "curl-argv"
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % bin_dir,
            "HOME": str(self.home),
            "YOLOBOX_AWS_BROKER_SOCK": BROKER_SOCK,
            "FAKE_CURL_ARGV": str(self.curl_argv),
            "FAKE_CURL_BODY": "",
            "FAKE_CURL_RC": "0",
        }

    def run_script(self, *args, curl_rc=0, curl_body=""):
        env = dict(self.env, FAKE_CURL_RC=str(curl_rc), FAKE_CURL_BODY=curl_body)
        return subprocess.run(
            [
                "bash",
                "-o",
                "errexit",
                "-o",
                "nounset",
                "-o",
                "pipefail",
                str(GUEST_SCRIPT),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            # A subcommand that loops forever (landing-dir on a relative path
            # once did) must fail the test, not hang the suite.
            timeout=20,
        )

    def curl_was_called(self):
        return self.curl_argv.exists()


class TestAwsCredsProfileName(GuestScript):
    def test_a_profile_name_that_is_not_a_plain_name_is_refused_before_curl_runs(self):
        for profile in ("-x", "../x", "a b", ".hidden", "a/b", "a?b", "dev\n", "a\nb"):
            with self.subTest(profile=profile):
                result = self.run_script("aws-creds", profile)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("unknown subcommand", result.stderr)
                self.assertNotEqual(result.stderr.strip(), "")
                self.assertFalse(self.curl_was_called())

    def test_an_empty_profile_name_fails_without_calling_curl(self):
        result = self.run_script("aws-creds", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unknown subcommand", result.stderr)
        self.assertFalse(self.curl_was_called())

    def test_a_missing_profile_argument_fails_without_calling_curl(self):
        result = self.run_script("aws-creds")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unknown subcommand", result.stderr)
        self.assertFalse(self.curl_was_called())


class TestAwsCredsBrokerAnswer(GuestScript):
    def test_success_prints_only_the_body_and_asks_the_broker_socket_for_the_profile(
        self,
    ):
        body = '{"Version": 1, "AccessKeyId": "AKIA"}'
        result = self.run_script("aws-creds", "my-prof.1_x", curl_body=body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, body + "\n")
        self.assertEqual(result.stderr, "")
        argv = self.curl_argv.read_text().split("\n")
        self.assertIn("http://broker/creds/my-prof.1_x", argv)
        self.assertEqual(argv[argv.index("--unix-socket") + 1], BROKER_SOCK)

    def test_curl_exit_7_52_and_56_say_to_run_yo_up(self):
        # 7: no socket unit; 52 and 56: systemd accepted and nothing answered.
        for rc in (7, 52, 56):
            with self.subTest(curl_exit=rc):
                result = self.run_script("aws-creds", "dev", curl_rc=rc)
                self.assertEqual(result.returncode, 1)
                self.assertIn("run `yo up`", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_any_other_curl_failure_passes_the_broker_error_to_stderr(self):
        result = self.run_script(
            "aws-creds", "dev", curl_rc=22, curl_body="SSO session expired"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("SSO session expired", result.stderr)
        self.assertNotIn("run `yo up`", result.stderr)


class TestLandingDir(GuestScript):
    def test_a_relative_path_is_refused(self):
        result = self.run_script("landing-dir", "foo")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("unknown subcommand", result.stderr)

    def test_a_missing_tail_lands_at_the_deepest_existing_ancestor(self):
        existing = self.tmp / "a"
        existing.mkdir()
        result = self.run_script("landing-dir", "%s/nonexistent/deep" % existing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "%s\n" % existing)

    def test_an_existing_path_lands_on_itself(self):
        result = self.run_script("landing-dir", str(self.tmp))
        self.assertEqual(result.stdout, "%s\n" % self.tmp)

    def test_a_path_with_no_existing_ancestor_lands_at_home(self):
        result = self.run_script("landing-dir", "/yolobox-test-no-such-root/deep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "%s\n" % self.home)


class TestUnknownSubcommand(GuestScript):
    def test_an_unknown_subcommand_exits_64_with_the_marker_yo_looks_for(self):
        result = self.run_script("bogus")
        self.assertEqual(result.returncode, 64)
        self.assertIn("unknown subcommand 'bogus'", result.stderr)
