"""yo's side of the AWS broker: which config it allow-lists from, and what it
does under its broker lock.

No tool's absence skips these tests.
"""

import fcntl
import os
import unittest
from unittest import mock

from test_yo import FakeHome, healthy, yo

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
        stale = healthy({"pid": 111, "watch_pid": 999, "allow": ["work"]})
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
                return_value=healthy({"pid": 111, "watch_pid": 999, "allow": []}),
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


if __name__ == "__main__":
    unittest.main()
