"""yo's side of the AWS broker: which config it allow-lists from, how the guest
config is installed, and the broker log's generations.

No tool's absence skips these tests.
"""

import fcntl
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from test_yo import (
    FakeHome,
    fake_bin,
    yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


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
    """Everything ensure_aws_broker reads and does happens under yo's broker
    lock, so two yos cannot interleave: one reading the pid or the allowlist
    before the other restarted the broker would replace a good broker or push a
    guest config that names other profiles than the broker serves."""

    def setUp(self):
        super().setUp()
        self.held_during = {}

    def lock_is_held(self):
        os.makedirs(yo.state.aws_broker_dir(), exist_ok=True)
        fd = os.open(
            os.path.join(yo.state.aws_broker_dir(), yo.aws.YO_BROKER_LOCK_NAME),
            os.O_RDWR | os.O_CREAT,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # drops the lock when this probe got it
        return False

    def seen(self, step, result=None):
        """A stand-in for `step` that records whether the lock is held when it runs."""

        def record(*args, **kwargs):
            self.held_during[step] = self.lock_is_held()
            return result

        return record

    def test_every_read_and_every_change_happens_under_the_lock(self):
        stale = {"pid": 111, "watch_pid": 999, "allow": ["work"]}
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
            mock.patch.object(yo.aws, "stop_broker", side_effect=self.seen("stop")),
            mock.patch.object(
                yo.aws, "start_aws_broker", side_effect=self.seen("start")
            ),
            mock.patch.object(
                yo.aws, "push_aws_guest_files", side_effect=self.seen("push")
            ),
        ):
            self.assertTrue(yo.ensure_aws_broker())
        self.assertEqual(
            self.held_during,
            {"allowlist": True, "pid": True, "stop": True, "start": True, "push": True},
        )
        self.assertFalse(self.lock_is_held())

    def test_disabling_for_an_empty_allowlist_happens_under_the_lock(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(yo.aws, "aws_allowlist", return_value=[]),
            mock.patch.object(
                yo.aws,
                "aws_broker_health",
                return_value={"pid": 111, "watch_pid": 999, "allow": []},
            ),
            mock.patch.object(yo.aws, "stop_broker", side_effect=self.seen("stop")),
            mock.patch.object(yo.sshx, "ssh_run", side_effect=self.seen("rm")),
            mock.patch.object(yo.ui, "err"),
        ):
            self.assertFalse(yo.ensure_aws_broker())
        self.assertEqual(self.held_during, {"stop": True, "rm": True})
        self.assertFalse(self.lock_is_held())

    def test_a_failing_step_releases_the_lock(self):
        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", side_effect=yo.YoError("unreadable")
            ),
        ):
            with self.assertRaises(yo.YoError):
                yo.ensure_aws_broker()
        self.assertFalse(self.lock_is_held())


class TestEachBoxHasItsOwnBroker(FakeHome):
    """A broker watches one box's keepalive process, so a session command on
    one box must leave another box's broker serving it."""

    def ensure_on(self, name, keepalive_pid):
        yo.set_box(yo.box.driver, name)
        try:
            with mock.patch.object(
                yo.box.driver, "keepalive_pid", return_value=keepalive_pid
            ):
                return yo.ensure_aws_broker()
        finally:
            yo.set_box(yo.box.driver, yo.DEFAULT_INSTANCE)

    def test_a_session_on_a_second_box_leaves_the_first_boxs_broker_alone(self):
        brokers = {}  # control socket -> /health of the broker listening there

        def start(watch_pid, allow):
            brokers[yo.state.aws_broker_ctl_sock()] = {
                "pid": 4000 + watch_pid,
                "watch_pid": watch_pid,
                "allow": list(allow),
            }

        with (
            mock.patch.object(yo.shutil, "which", return_value="/usr/local/bin/aws"),
            mock.patch.object(
                yo.aws, "aws_allowlist", return_value=[("work", "us-east-1")]
            ),
            mock.patch.object(
                yo.aws, "health_on", side_effect=lambda path, _: brokers.get(path)
            ),
            mock.patch.object(yo.aws, "start_aws_broker", side_effect=start),
            mock.patch.object(yo.aws, "stop_broker") as stop,
            mock.patch.object(yo.aws, "push_aws_guest_files"),
        ):
            self.assertTrue(self.ensure_on(yo.DEFAULT_INSTANCE, 111))
            self.assertTrue(self.ensure_on("e2e1", 222))
            self.assertTrue(self.ensure_on(yo.DEFAULT_INSTANCE, 111))
        stop.assert_not_called()
        self.assertEqual(
            sorted((health["watch_pid"], path) for path, health in brokers.items()),
            [
                (111, os.path.join(self.home, ".lima/yolobox/aws-broker/ctl.sock")),
                (222, os.path.join(self.home, ".lima/e2e1/aws-broker/ctl.sock")),
            ],
        )


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
        _, path = fake_bin(self, {name: "#!/bin/sh\nexit 1\n" for name in failing})
        return subprocess.run(
            yo.aws.push_config_argv(self.config, text),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={**os.environ, "PATH": path},
        )

    def leftovers(self):
        return sorted(os.listdir(os.path.dirname(self.config)))

    def test_the_text_is_installed_verbatim_whatever_it_holds(self):
        for text in ["", "a\n", '[profile x]\nregion = %s\n\'"$tmp" \\ é\n']:
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


class TestAHiddenGuestAwsConfigIsNamed(FakeHome):
    """After the push, yo asks whether the agent's own ~/.aws/config exists,
    which AWS_CONFIG_FILE hides, and tells only a named answer from none."""

    def push(self, answer):
        warnings = []

        def ssh_run(role, argv, **kwargs):
            if argv[:2] == ["sh", "-c"]:
                return yo.proc.GuestRun(argv, *answer)
            return yo.proc.GuestRun(argv, 0, "", "")

        with (
            mock.patch.object(yo.sshx, "ssh_run", ssh_run),
            mock.patch.object(yo.ui, "err", warnings.append),
        ):
            yo.aws.push_aws_guest_files([("work", "us-east-1")])
        return warnings

    def test_an_existing_config_is_named_and_a_missing_one_is_not(self):
        (warning,) = self.push((0, "present\n", ""))
        self.assertIn("/home/agent/.aws/config exists in the VM", warning)
        self.assertEqual(self.push((0, "absent\n", "")), [])

    def test_an_answer_that_names_neither_is_an_error_showing_it(self):
        for answer in ((0, "lizard\n", ""), (0, "", ""), (1, "", "zsh: boom")):
            with self.subTest(answer=answer):
                with self.assertRaises(yo.YoError) as caught:
                    self.push(answer)
                self.assertNotIsInstance(caught.exception, yo.Unreachable)

    def test_a_box_out_of_reach_is_unreachable(self):
        with self.assertRaises(yo.Unreachable):
            self.push((yo.SSH_UNREACHABLE, "", "ssh: Connection refused"))


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
        return os.path.join(yo.state.aws_broker_dir(), "broker.log" + suffix)

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
