"""yo's side of the AWS broker: which config it allow-lists from, and what it
does under its broker lock.

No tool's absence skips these tests.
"""

import fcntl
import os
import shutil
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from test_aws_broker import broker
from test_yo import FakeHome, healthy, yo


def broker_health(pid, watch_pid, allow):
    """A healthy broker whose /health body is what the broker's own handler
    answers, so a key the broker renames fails here, not on a Mac."""
    handler = types.SimpleNamespace(watch_pid=watch_pid, states=dict.fromkeys(allow))
    with mock.patch.object(broker.os, "getpid", return_value=pid):
        return healthy(broker.HealthHandler.health(handler))


SSO_PROFILE = "[profile %s]\nsso_start_url = https://example.awsapps.com/start\nregion = us-east-1\n"


class TestAllowlistConfigFile(FakeHome):
    """The allowlist comes from the file the host's `aws` reads, so that yo and
    the broker's `aws configure export-credentials` agree on the profiles."""

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_the_default_is_the_homes_aws_config(self):
        self.write(os.path.join(self.home, ".aws", "config"), SSO_PROFILE % "home")
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": ""}):
            self.assertEqual(yo.aws_allowlist(), [("home", "us-east-1")])
        with mock.patch.dict(os.environ):
            os.environ.pop("AWS_CONFIG_FILE", None)
            self.assertEqual(yo.aws_allowlist(), [("home", "us-east-1")])

    def test_aws_config_file_names_the_file_and_the_homes_is_then_ignored(self):
        self.write(os.path.join(self.home, ".aws", "config"), SSO_PROFILE % "home")
        other = self.write(
            os.path.join(self.home, "elsewhere.ini"), SSO_PROFILE % "work"
        )
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": other}):
            self.assertEqual(yo.aws_allowlist(), [("work", "us-east-1")])

    def test_a_tilde_in_aws_config_file_is_the_home(self):
        self.write(os.path.join(self.home, "elsewhere.ini"), SSO_PROFILE % "work")
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": "~/elsewhere.ini"}):
            self.assertEqual(yo.aws_allowlist(), [("work", "us-east-1")])

    def test_a_missing_named_file_is_an_empty_allowlist(self):
        missing = os.path.join(self.home, "nowhere.ini")
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": missing}):
            self.assertEqual(yo.aws_allowlist(), [])

    def test_warnings_name_the_file_that_was_read(self):
        other = self.write(
            os.path.join(self.home, "elsewhere.ini"),
            "[profile noregion]\nsso_start_url = https://example.awsapps.com/start\n",
        )
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": other}):
            with mock.patch.object(yo.ui, "err") as err:
                self.assertEqual(yo.aws_allowlist(), [])
        message = err.call_args.args[0]
        self.assertIn("~/elsewhere.ini", message)
        self.assertNotIn("~/.aws/config", message)

    def test_an_unparseable_named_file_is_a_yoerror_naming_it(self):
        other = self.write(os.path.join(self.home, "elsewhere.ini"), "[a]\n[a]\n")
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": other}):
            with self.assertRaises(yo.YoError) as caught:
                yo.aws_allowlist()
        self.assertIn(other, caught.exception.message)


class TestEnsureAwsBrokerIsOneStep(FakeHome):
    """Everything ensure_aws_broker reads and writes happens under its lock, so
    two yos cannot interleave: one reading the pid or the allowlist before the
    other restarted the broker would replace a good broker or push a guest
    config that names other profiles than the broker serves."""

    def setUp(self):
        super().setUp()
        self.held_during = {}

    def lock_is_held(self):
        os.makedirs(yo.state_dir(), exist_ok=True)
        fd = os.open(
            os.path.join(yo.state_dir(), yo.aws.YO_BROKER_LOCK_NAME),
            os.O_RDWR | os.O_CREAT,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def seen(self, step, result=None):
        def record(*args, **kwargs):
            self.held_during[step] = self.lock_is_held()
            return result

        return record

    def test_the_allowlist_the_pid_and_the_push_are_read_and_done_under_the_lock(
        self,
    ):
        stale = broker_health(pid=111, watch_pid=999, allow=["work"])
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws,
                "aws_allowlist",
                side_effect=self.seen("allowlist", [("work", "us-east-1")]),
            ),
            mock.patch.object(
                yo.box.driver, "keepalive_pid", side_effect=self.seen("pid", 555)
            ),
            mock.patch.object(yo.aws, "aws_broker_health", return_value=stale),
            mock.patch.object(yo.aws, "kill_stale_broker"),
            mock.patch.object(yo.aws, "start_aws_broker"),
            mock.patch.object(
                yo.aws, "push_aws_guest_files", side_effect=self.seen("push")
            ),
        ):
            self.assertTrue(yo.ensure_aws_broker())
        self.assertEqual(
            self.held_during, {"allowlist": True, "pid": True, "push": True}
        )
        self.assertFalse(self.lock_is_held())

    def test_disabling_removes_the_guest_config_under_the_same_lock(self):
        with (
            mock.patch.object(
                yo.aws,
                "aws_broker_health",
                return_value=broker_health(pid=111, watch_pid=999, allow=[]),
            ),
            mock.patch.object(yo.aws, "kill_stale_broker"),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=self.seen("rm")),
        ):
            yo.disable_aws_broker()
        self.assertEqual(self.held_during, {"rm": True})
        self.assertFalse(self.lock_is_held())

    def test_an_empty_allowlist_releases_the_lock_before_disabling(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(yo.aws, "aws_allowlist", return_value=[]),
            mock.patch.object(
                yo.aws, "disable_aws_broker", side_effect=self.seen("disable")
            ),
            mock.patch.object(yo.ui, "err"),
        ):
            self.assertFalse(yo.ensure_aws_broker())
        self.assertEqual(self.held_during, {"disable": False})


class TestPushConfigCommand(unittest.TestCase):
    """The guest-side command that installs the rendered AWS config: the whole
    text or nothing, atomically, with no temp file left on any failure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config = os.path.join(self.tmp, "cfg", "aws-config")

    def push(self, text, failing=()):
        """Run the command under sh with stdin at EOF, the shape of a dropped
        ssh; `failing` names commands replaced by ones that exit 1."""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        for name in failing:
            fake = os.path.join(bindir, name)
            with open(fake, "w") as handle:
                handle.write("#!/bin/sh\nexit 1\n")
            os.chmod(fake, 0o755)
        return subprocess.run(
            yo.aws.push_config_argv(self.config, text),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={**os.environ, "PATH": bindir + ":" + os.environ["PATH"]},
        )

    def leftovers(self):
        return sorted(os.listdir(os.path.dirname(self.config)))

    def test_the_text_is_installed_verbatim_whatever_it_holds(self):
        for text in ["", "a\n", '[profile x]\nregion = %s\n\'"$tmp" \\ \u00e9\n']:
            with self.subTest(text=text):
                done = self.push(text)
                self.assertEqual(done.returncode, 0, done.stderr)
                with open(self.config) as handle:
                    self.assertEqual(handle.read(), text)
                self.assertEqual(self.leftovers(), ["aws-config"])

    def test_a_failed_install_leaves_no_temp_file_and_no_config(self):
        done = self.push("[profile x]\n", failing=["mv"])
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_install_keeps_the_previous_config(self):
        self.assertEqual(self.push("old\n").returncode, 0)
        self.assertNotEqual(self.push("new\n", failing=["mv"]).returncode, 0)
        with open(self.config) as handle:
            self.assertEqual(handle.read(), "old\n")
        self.assertEqual(self.leftovers(), ["aws-config"])


class TestBrokerLogGenerations(FakeHome):
    """A restart keeps the previous broker's log as broker.log.1."""

    def start(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 0
        with (
            mock.patch.object(yo.subprocess, "Popen", return_value=proc),
            mock.patch.object(
                yo.aws,
                "read_handshake",
                return_value=yo.aws.Handshake(["READY"], False),
            ),
        ):
            yo.start_aws_broker(555, ["work"])

    def log(self, suffix=""):
        return os.path.join(yo.state_dir(), "aws-broker", "broker.log" + suffix)

    def test_a_first_start_makes_no_previous_generation(self):
        self.start()
        self.assertTrue(os.path.exists(self.log()))
        self.assertFalse(os.path.exists(self.log(".1")))

    def test_the_previous_log_is_kept_one_generation_and_stays_private(self):
        os.makedirs(os.path.dirname(self.log()))
        for run in ["first", "second", "third"]:
            with open(self.log(), "w") as handle:
                handle.write(run)
            os.chmod(self.log(), 0o600)
            self.start()
        with open(self.log(".1")) as handle:
            self.assertEqual(handle.read(), "third")
        self.assertEqual(os.path.getsize(self.log()), 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.log(".1")).st_mode), 0o600)
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(self.log()))),
            ["broker.log", "broker.log.1"],
        )


if __name__ == "__main__":
    unittest.main()
