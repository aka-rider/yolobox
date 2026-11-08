"""The AWS broker's request handler, over a real unix socket.

Tools whose absence skips tests: a Python with `select.kqueue` or
`os.pidfd_open` (the pid-watch classes).
"""

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import select
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BROKER_PATH = str(ROOT / "aws-broker")


def load_broker():
    loader = importlib.machinery.SourceFileLoader("aws_broker", BROKER_PATH)
    spec = importlib.util.spec_from_loader("aws_broker", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["aws_broker"] = module
    loader.exec_module(module)
    return module


broker = load_broker()
from yolobox_cli import common  # noqa: E402

HAS_PID_WATCH = hasattr(select, "kqueue") or hasattr(os, "pidfd_open")
# ServedBroker replaces broker.log with a mock; these keep the real writer.
REAL_LOG = broker.log


def expires_in(seconds):
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def future(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def process_json(**overrides):
    data = {
        "Version": 1,
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": "s3cr3t-value",
        "SessionToken": "token-value",
        "Expiration": future(),
    }
    data.update(overrides)
    return json.dumps(data)


def aws_result(stdout="", stderr="", returncode=0):
    """What `aws` printed, as the bytes run_aws hands back; text is UTF-8."""
    return subprocess.CompletedProcess(
        [],
        returncode,
        stdout=stdout.encode() if isinstance(stdout, str) else stdout,
        stderr=stderr.encode() if isinstance(stderr, str) else stderr,
    )


def broker_argv(sock, ctl, watch_pid=None):
    return [
        sys.executable,
        BROKER_PATH,
        *["--socket", sock, "--control-socket", ctl],
        *["--watch-pid", str(watch_pid or os.getpid()), "--allow", "dev"],
        *["--log", broker_log_path(sock)],
    ]


def broker_log_path(sock):
    return os.path.join(os.path.dirname(sock), "broker.log")


class TestProfileNamePattern(unittest.TestCase):
    def test_accepts_ordinary_names(self):
        for name in ["dev", "A", "0", "prod-eu_1.2", "a.b"]:
            with self.subTest(name=name):
                self.assertTrue(broker.PROFILE_NAME_RE.fullmatch(name))

    def test_rejects_hostile_names(self):
        for name in [
            "",
            "-x",
            "--profile",
            ".hidden",
            "_x",
            "a b",
            "a\n",
            "\na",
            "a/b",
            "a/../b",
            "..",
            "a;b",
            "lizardé",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(broker.PROFILE_NAME_RE.fullmatch(name))


class TestCredsProfile(unittest.TestCase):
    def test_the_profile_is_whatever_follows_the_creds_prefix(self):
        for path, profile in [
            ("/creds/dev", "dev"),
            ("/creds/", ""),
            ("/creds/a/b?x=1", "a/b?x=1"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(broker.creds_profile(path), profile)

    def test_any_other_path_names_no_profile(self):
        for path in ["/health", "/creds", "/credsdev", "", "creds/dev", "/x/creds/dev"]:
            with self.subTest(path=path):
                self.assertIsNone(broker.creds_profile(path))


class TestSanitize(unittest.TestCase):
    def test_every_control_character_but_tab_and_newline_becomes_a_question_mark(self):
        controls = {*range(0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0)}
        for code in range(0x200):
            char = chr(code)
            expected = "?" if code in controls else char
            with self.subTest(code=hex(code)):
                self.assertEqual(broker.sanitize(char), expected)


class TestParseArgs(unittest.TestCase):
    def parse(self, *allow):
        argv = ["aws-broker", "--socket", "/x", "--control-socket", "/y"]
        argv += ["--watch-pid", "1", "--log", "/z", "--allow", *allow]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            try:
                return broker.parse_args(), out.getvalue()
            except SystemExit as e:
                return e, out.getvalue()

    def test_valid_names_pass(self):
        args, _ = self.parse("dev", "prod-1")
        self.assertEqual(args.allow, ["dev", "prod-1"])

    def test_invalid_name_fails_with_handshake_error_line(self):
        for name in [".hidden", "_x", "a b", "a\nb", "a\n", ".."]:
            with self.subTest(name=name):
                exit_, out = self.parse("ok", name)
                self.assertIsInstance(exit_, SystemExit)
                self.assertEqual(exit_.code, 1)
                self.assertTrue(out.startswith("ERROR=invalid profile name"))
                self.assertEqual(out.count("\n"), 1)


class TestParseExpiration(unittest.TestCase):
    def test_every_shape_botocore_returns(self):
        expected = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        for text in [
            "2030-01-02T03:04:05Z",
            "2030-01-02T03:04:05+00:00",
            "2030-01-02T03:04:05+0000",
            "2030-01-02T03:04:05",
            "2030-01-02T03:04:05.000Z",
            "2030-01-02T03:04:05.0Z",
            "2030-01-02T03:04:05.0000000009Z",
            "2030-01-02T04:04:05+01:00",
        ]:
            with self.subTest(text=text):
                self.assertEqual(broker.parse_expiration(text), expected)

    def test_fraction_width_is_kept_not_rounded(self):
        got = broker.parse_expiration("2030-01-02T03:04:05.5Z")
        self.assertEqual(got.microsecond, 500000)

    def test_long_fraction_is_truncated_to_microseconds(self):
        got = broker.parse_expiration("2030-01-02T03:04:05.123456789Z")
        self.assertEqual(got.microsecond, 123456)

    def test_naive_timestamp_is_utc(self):
        self.assertEqual(
            broker.parse_expiration("2030-01-02T03:04:05").utcoffset(), timedelta(0)
        )

    def test_garbage_raises_value_error(self):
        for text in [
            "",
            "tomorrow",
            "2030-01-02",
            "2030-01-02T03:04:05ZZ",
            "9999-99-99T99:99:99Z",
        ]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    broker.parse_expiration(text)


class TestExportCredentials(unittest.TestCase):
    def run_export(self, result):
        with mock.patch.object(broker, "run_aws", return_value=result):
            return broker.export_credentials("dev")

    def test_good_output_returns_the_four_fields(self):
        creds, err = self.run_export(aws_result(process_json()))
        self.assertIsNone(err)
        self.assertEqual(
            sorted(creds),
            ["AccessKeyId", "Expiration", "SecretAccessKey", "SessionToken"],
        )

    def test_expiration_is_served_as_utc_with_a_zone_whatever_the_cli_returned(self):
        for text in [
            "2099-01-01T00:00:00",
            "2099-01-01T00:00:00Z",
            "2099-01-01T00:00:00+00:00",
            "2099-01-01T00:00:00.123456789Z",
            "2099-01-01T01:00:00+01:00",
            "2098-12-31T19:00:00-0500",
        ]:
            with self.subTest(text=text):
                creds, err = self.run_export(aws_result(process_json(Expiration=text)))
                self.assertIsNone(err)
                self.assertEqual(creds["Expiration"], "2099-01-01T00:00:00Z")

    def test_a_hung_cli_is_killed_after_the_mint_timeout_and_the_message_says_so(self):
        timed_out = subprocess.TimeoutExpired("aws", 7)
        with mock.patch.object(broker, "MINT_TIMEOUT_SECONDS", 7):
            with mock.patch.object(broker, "run_aws", side_effect=timed_out):
                creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("timed out after 7s", err)

    def test_the_cli_never_gets_the_brokers_stdin(self):
        with tempfile.TemporaryDirectory() as bindir:
            fake = Path(bindir, "aws")
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os\n"
                "devnull = os.path.samestat(os.fstat(0), os.stat(os.devnull))\n"
                "print(json.dumps({'AccessKeyId': 'a', 'SecretAccessKey': 's',\n"
                "  'SessionToken': 'devnull' if devnull else 'inherited',\n"
                "  'Expiration': '2099-01-01T00:00:00Z'}))\n"
            )
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": bindir}):
                creds, err = broker.export_credentials("dev")
        self.assertIsNone(err)
        self.assertEqual(creds["SessionToken"], "devnull")

    def test_malformed_fields_become_errors_not_exceptions(self):
        for override in [
            {"Expiration": None},
            {"Expiration": 12345},
            {"Expiration": ""},
            {"Expiration": "soon"},
            {"AccessKeyId": ""},
            {"AccessKeyId": None},
            {"SecretAccessKey": 7},
            {"SessionToken": ""},
        ]:
            with self.subTest(override=override):
                creds, err = self.run_export(aws_result(process_json(**override)))
                self.assertIsNone(creds)
                self.assertTrue(err)

    def test_missing_field_is_an_error(self):
        data = json.loads(process_json())
        del data["AccessKeyId"]
        creds, err = self.run_export(aws_result(json.dumps(data)))
        self.assertIsNone(creds)
        self.assertIn("AccessKeyId", err)

    def test_long_lived_keys_are_refused(self):
        data = json.loads(process_json())
        del data["SessionToken"]
        creds, err = self.run_export(aws_result(json.dumps(data)))
        self.assertIsNone(creds)
        self.assertIn("long-lived", err)

    def test_non_object_json_is_an_error(self):
        for text in ["[]", "null", '"x"', "3", "not json", ""]:
            with self.subTest(text=text):
                creds, err = self.run_export(aws_result(text))
                self.assertIsNone(creds)
                self.assertTrue(err)

    def test_error_text_never_carries_secret_values(self):
        for name, result in [
            ("unusable expiration", aws_result(process_json(Expiration="soon"))),
            (
                "a failure that printed the credentials",
                aws_result(process_json(), returncode=1),
            ),
            (
                "a failure with stderr and the credentials",
                aws_result(process_json(), stderr="warning: x", returncode=1),
            ),
        ]:
            with self.subTest(name):
                creds, err = self.run_export(result)
                self.assertIsNone(creds)
                for secret in ["s3cr3t-value", "token-value", "AKIAEXAMPLE"]:
                    self.assertNotIn(secret, err)

    def test_a_failure_without_stderr_says_how_much_stdout_there_was(self):
        _, err = self.run_export(aws_result("12345", returncode=1))
        self.assertIn("exit 1", err)
        self.assertIn("5 bytes of stdout", err)
        self.assertNotIn("12345", err)


# A stand-in for `aws` that hangs, with a grandchild that holds the FIFO named
# by $MINT_FIFO open: a reader of the FIFO sees EOF only once the grandchild
# is dead.
HUNG_AWS_WITH_GRANDCHILD = """#!/bin/sh
sh -c 'exec 3>"$MINT_FIFO"; echo started >&3; exec sleep 600' &
exec sleep 600
"""


class MintGrandchild(unittest.TestCase):
    """A fake aws on PATH whose grandchild reports through a FIFO."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        fake = Path(self.tmp, "aws")
        fake.write_text(HUNG_AWS_WITH_GRANDCHILD)
        fake.chmod(0o755)
        self.fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(self.fifo)
        self.env = {"PATH": self.tmp + ":" + os.environ["PATH"], "MINT_FIFO": self.fifo}

    def grandchild_started(self):
        """Block until the grandchild opened the FIFO; keep the read end."""
        self.reader = os.open(self.fifo, os.O_RDONLY)
        self.addCleanup(os.close, self.reader)
        self.assertEqual(os.read(self.reader, 8), b"started\n")

    def assert_grandchild_dead(self):
        ready, _, _ = select.select([self.reader], [], [], 10)
        self.assertEqual(ready, [self.reader], "the grandchild still holds the FIFO")
        self.assertEqual(os.read(self.reader, 8), b"")


class TestMintTimeoutEndsTheWholeProcessGroup(MintGrandchild):
    def test_the_grandchild_of_a_timed_out_mint_is_dead_too(self):
        real_communicate = subprocess.Popen.communicate

        def communicate_once_the_grandchild_runs(proc, *args, **kwargs):
            # The clock is consulted only after the grandchild reported in, so
            # no wait for it is raced against the mint timeout.
            self.grandchild_started()
            return real_communicate(proc, *args, **{**kwargs, "timeout": 0})

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(
                subprocess.Popen, "communicate", communicate_once_the_grandchild_runs
            ):
                creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("timed out", err)
        self.assert_grandchild_dead()
        self.assertIsNone(broker.mint_in_flight)


class TestMintOutputThatIsNotUtf8(unittest.TestCase):
    def test_a_failure_with_non_utf8_output_is_an_error_not_an_exception(self):
        with mock.patch.object(
            broker,
            "run_aws",
            return_value=aws_result(returncode=255, stderr=b"bad \xff\xfe byte"),
        ):
            creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("bad �� byte", err)

    def test_a_success_with_non_utf8_output_is_an_error_not_an_exception(self):
        with mock.patch.object(broker, "run_aws", return_value=aws_result(b"\xff\xfe")):
            creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("unparseable", err)

    def test_the_remint_throttle_still_counts_a_mint_that_printed_non_utf8(self):
        cache = broker.ProfileCache("dev")
        with (
            mock.patch.object(broker, "log"),
            mock.patch.object(broker, "MIN_REMINT_INTERVAL_SECONDS", 3600.0),
            mock.patch.object(
                broker,
                "run_aws",
                return_value=aws_result(returncode=255, stderr=b"\xff"),
            ) as run,
        ):
            self.assertIsNone(cache.get())
            self.assertIsNone(cache.get())
        self.assertEqual(run.call_count, 1)


class SocketDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="awsb")
        self.addCleanup(self.rmtree)
        self.sock = os.path.join(self.dir, "s.sock")
        self.ctl = os.path.join(self.dir, "c.sock")

    def rmtree(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def fails(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                fn(*args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(out.getvalue().startswith("ERROR="))
        return out.getvalue()

    def spawn_broker(self, argv, **popen_kwargs):
        """A broker process whose pipes and life end with the test."""
        child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs,
        )
        self.addCleanup(child.stderr.close)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def script_argv(self, script):
        """Run `script` (LOAD_BROKER ... RUN_BROKER) on this test's sockets."""
        return [
            *[sys.executable, "-c", script, BROKER_PATH, self.sock],
            *[str(os.getpid()), broker_log_path(self.sock)],
        ]

    def start_broker(self, **popen_kwargs):
        """A broker on this test's sockets, running and READY."""
        child = self.spawn_broker(broker_argv(self.sock, self.ctl), **popen_kwargs)
        self.assertEqual(child.stdout.readline(), "READY\n", child.stderr)
        return child


class TestReclaimSocketPath(SocketDirCase):
    def test_missing_path_is_fine(self):
        broker.reclaim_socket_path(self.sock)

    def test_refuses_a_regular_file_and_leaves_it(self):
        Path(self.sock).write_text("precious")
        out = self.fails(broker.reclaim_socket_path, self.sock)
        self.assertIn("not a socket", out)
        self.assertEqual(Path(self.sock).read_text(), "precious")

    def test_refuses_a_symlink_and_leaves_it(self):
        target = os.path.join(self.dir, "target")
        Path(target).write_text("precious")
        os.symlink(target, self.sock)
        out = self.fails(broker.reclaim_socket_path, self.sock)
        self.assertIn("not a socket", out)
        self.assertTrue(os.path.islink(self.sock))
        self.assertEqual(Path(target).read_text(), "precious")

    def test_refuses_a_symlink_to_a_live_socket(self):
        real = os.path.join(self.dir, "real.sock")
        server, _ = broker.bind_guest_server(real, {}, 1)
        self.addCleanup(server.server_close)
        os.symlink(real, self.sock)
        out = self.fails(broker.reclaim_socket_path, self.sock)
        self.assertIn("not a socket", out)
        self.assertTrue(os.path.islink(self.sock))

    def test_removes_a_stale_socket(self):
        server, _ = broker.bind_guest_server(self.sock, {}, 1)
        server.server_close()  # closed but the file remains: stale
        self.assertTrue(os.path.exists(self.sock))
        broker.reclaim_socket_path(self.sock)
        self.assertFalse(os.path.exists(self.sock))

    def test_refuses_a_live_socket(self):
        server, _ = broker.bind_guest_server(self.sock, {}, 1)
        self.addCleanup(server.server_close)
        self.fails(broker.reclaim_socket_path, self.sock)
        self.assertTrue(os.path.exists(self.sock))


class TestUnlinkIfSameIdentity(SocketDirCase):
    def test_removes_the_file_with_that_identity(self):
        server, identity = broker.bind_guest_server(self.sock, {}, 1)
        server.server_close()
        broker.unlink_if_same_identity(self.sock, identity)
        self.assertFalse(os.path.lexists(self.sock))

    def test_leaves_a_newer_file_alone(self):
        server, identity = broker.bind_guest_server(self.sock, {}, 1)
        server.server_close()
        # Made while the old socket still exists, then renamed over it: Linux
        # hands a freed inode number straight to the next file, which would
        # give the newer file the old identity. (The broker runs on APFS,
        # which does not reuse inode numbers.)
        newer = os.path.join(self.dir, "newer")
        Path(newer).write_text("newer broker")
        os.rename(newer, self.sock)
        with mock.patch.object(broker, "log") as log:
            broker.unlink_if_same_identity(self.sock, identity)
        self.assertTrue(os.path.exists(self.sock))
        self.assertIn("not the socket this broker bound", log.call_args.args[0])

    def test_a_path_that_cannot_be_inspected_is_logged_not_ignored(self):
        server, identity = broker.bind_guest_server(self.sock, {}, 1)
        server.server_close()
        with mock.patch.object(broker.os, "lstat", side_effect=PermissionError("no")):
            with mock.patch.object(broker, "log") as log:
                broker.unlink_if_same_identity(self.sock, identity)
        self.assertIn("cannot stat", log.call_args.args[0])
        self.assertIn("no", log.call_args.args[0])
        self.assertTrue(os.path.lexists(self.sock))

    def test_a_failing_unlink_is_logged_not_ignored(self):
        server, identity = broker.bind_guest_server(self.sock, {}, 1)
        server.server_close()
        with mock.patch.object(broker.os, "unlink", side_effect=PermissionError("no")):
            with mock.patch.object(broker, "log") as log:
                broker.unlink_if_same_identity(self.sock, identity)
        self.assertIn("cannot remove", log.call_args.args[0])
        self.assertIn("no", log.call_args.args[0])

    def test_does_not_follow_a_symlink_to_the_identity(self):
        real = os.path.join(self.dir, "real.sock")
        server, identity = broker.bind_guest_server(real, {}, 1)
        server.server_close()
        os.symlink(real, self.sock)
        with mock.patch.object(broker, "log") as log:
            broker.unlink_if_same_identity(self.sock, identity)
        self.assertIn("not the socket this broker bound", log.call_args.args[0])
        self.assertTrue(os.path.islink(self.sock))
        self.assertTrue(os.path.exists(real))


class WaitingChild(unittest.TestCase):
    """A child process that lives until its stdin closes."""

    def setUp(self):
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.stdin.close)


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestPidWatch(WaitingChild):
    def test_the_wait_blocks_while_the_process_runs_and_returns_when_it_exits(self):
        wait = broker.register_pid_watch_event(self.child.pid)
        self.assertFalse(wait(0), "the wait reported an exit while the process ran")
        returned = threading.Event()

        def block_until_exit():
            wait()
            returned.set()

        threading.Thread(target=block_until_exit, daemon=True).start()
        self.child.stdin.close()
        self.assertTrue(returned.wait(30), "the wait never saw the process exit")

    def test_a_pid_that_is_gone_refuses_to_register(self):
        self.child.stdin.close()
        self.child.wait()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                broker.register_pid_watch_event(self.child.pid)
        self.assertIn("is not running", out.getvalue())


class TestNoPidWatchMechanism(unittest.TestCase):
    """A Python with neither kqueue nor pidfd_open (uv's 3.9 build)."""

    def setUp(self):
        for module in (common.select, common.os):
            patch = mock.patch.object(common, module.__name__, types.SimpleNamespace())
            patch.start()
            self.addCleanup(patch.stop)

    def test_pid_exit_watch_says_what_is_missing(self):
        with self.assertRaisesRegex(OSError, "neither select.kqueue nor os.pidfd_open"):
            common.pid_exit_watch(1)

    def test_the_broker_refuses_to_start_with_that_message(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                broker.register_pid_watch_event(1)
        self.assertRegex(
            out.getvalue(), r"^ERROR=registration of a watch on pid 1 failed: .*neither"
        )


class Exited(Exception):
    """Stands in for os._exit, which would end the test run."""


class TestWatchedPidExit(unittest.TestCase):
    def run_wait(self, wait_for_exit, status):
        with (
            mock.patch.object(broker, "log") as log,
            mock.patch.object(broker.os, "_exit", side_effect=Exited) as exit_,
        ):
            with self.assertRaises(Exited):
                gone = broker.BoundSocket("/no/such/socket", (0, 0))
                broker.wait_for_pid_exit(7, [gone], wait_for_exit)
        exit_.assert_called_once_with(status)
        return "\n".join(str(call.args[0]) for call in log.call_args_list)

    def test_an_exit_is_logged_and_shuts_the_broker_down(self):
        self.assertIn("watched pid 7 exited", self.run_wait(lambda: None, 0))

    def test_a_failing_wait_is_logged_as_a_failure_and_exits_non_zero(self):
        def broken():
            raise OSError("kevent exploded")

        logged = self.run_wait(broken, 1)
        self.assertIn("waiting for pid 7 failed: kevent exploded", logged)
        self.assertNotIn("exited", logged)


class TestMain(SocketDirCase):
    def test_a_watch_that_cannot_be_set_up_leaves_no_socket_behind(self):
        argv = ["aws-broker", "--socket", self.sock, "--control-socket", self.ctl]
        argv += ["--watch-pid", str(os.getpid()), "--log", broker_log_path(self.sock)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [*argv, "--allow", "dev"]):
            with mock.patch.object(broker, "pid_exit_watch", side_effect=OSError("no")):
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                    broker.main()
        self.assertIn("ERROR=registration of a watch", out.getvalue())
        self.assertFalse(os.path.lexists(self.sock))

    @unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
    def test_a_watched_pid_that_is_not_running_is_a_handshake_error(self):
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        argv = ["aws-broker", "--socket", self.sock, "--control-socket", self.ctl]
        argv += ["--watch-pid", str(gone.pid), "--allow", "dev"]
        argv += ["--log", broker_log_path(self.sock)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                broker.main()
        self.assertEqual(
            out.getvalue(), f"ERROR=watched pid {gone.pid} is not running\n"
        )
        self.assertFalse(os.path.lexists(self.sock))
        self.assertFalse(os.path.lexists(self.ctl))

    def test_a_socket_lock_that_cannot_be_taken_is_a_handshake_error(self):
        os.mkdir(self.sock + ".lock")
        self.addCleanup(os.rmdir, self.sock + ".lock")
        done = subprocess.run(
            broker_argv(self.sock, self.ctl), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertTrue(done.stdout.startswith("ERROR=cannot lock"), done.stdout)
        self.assertFalse(os.path.lexists(self.sock))


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSigterm(SocketDirCase):
    def test_sigterm_removes_both_sockets_and_exits_cleanly(self):
        child = self.start_broker()
        self.assertTrue(os.path.exists(self.sock))
        self.assertTrue(os.path.exists(self.ctl))
        child.terminate()
        self.assertEqual(child.wait(30), 0, child.stderr.read())
        self.assertFalse(os.path.lexists(self.sock))
        self.assertFalse(os.path.lexists(self.ctl))


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSecondBroker(SocketDirCase):
    def test_a_second_broker_on_a_live_socket_fails_and_leaves_the_first_serving(
        self,
    ):
        first = self.start_broker()
        done = subprocess.run(
            broker_argv(self.sock, self.ctl), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            done.stdout, "ERROR=another aws-broker is already serving %s\n" % self.sock
        )
        self.assertIsNone(first.poll())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(self.sock)


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestStoppedBrokerEndsItsMints(MintGrandchild, SocketDirCase):
    def setUp(self):
        MintGrandchild.setUp(self)
        SocketDirCase.setUp(self)

    def test_sigterm_ends_a_mint_in_flight_and_its_grandchild(self):
        child = self.start_broker(env={**os.environ, **self.env})
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(self.sock)
        client.sendall(b"GET /creds/dev HTTP/1.0\r\n\r\n")
        self.grandchild_started()
        child.terminate()
        self.assertEqual(child.wait(30), 0, child.stderr.read())
        self.assert_grandchild_dead()


# The broker file loaded as a module, a patch of it, then main(). A script's
# arguments: the broker file, the guest socket, the watched pid, the log.
LOAD_BROKER = """
import importlib.machinery, importlib.util, os, signal, sys
loader = importlib.machinery.SourceFileLoader("aws_broker", sys.argv[1])
broker = importlib.util.module_from_spec(importlib.util.spec_from_loader("aws_broker", loader))
loader.exec_module(broker)
"""
RUN_BROKER = """
sys.argv = ["aws-broker", "--socket", sys.argv[2], "--control-socket", sys.argv[2] + ".ctl",
            "--watch-pid", sys.argv[3], "--allow", "dev", "--log", sys.argv[4]]
broker.main()
"""

# SIGTERM lands right after the bind.
SIGTERM_AFTER_BIND_SCRIPT = (
    LOAD_BROKER
    + """
real_bind = broker.bind_servers

def bind_then_sigterm(*args, **kwargs):
    bound = real_bind(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGTERM)  # lands between the bind and main()'s next line
    return bound

broker.bind_servers = bind_then_sigterm
"""
    + RUN_BROKER
)

# SIGTERM lands inside the log's write, on the thread that holds its lock.
SIGTERM_IN_LOG_WRITE_SCRIPT = (
    LOAD_BROKER
    + """
real_fstat, fired = os.fstat, []

def fstat_then_sigterm(fd):
    if fd == 2 and not fired:  # CappedLog.write, under its lock
        fired.append(True)
        os.kill(os.getpid(), signal.SIGTERM)
    return real_fstat(fd)

os.fstat = fstat_then_sigterm
"""
    + RUN_BROKER
)

# Stops between the reclaim and the bind until stdin says go.
PAUSE_SCRIPT = (
    LOAD_BROKER
    + """
real_bind = broker.bind_servers

def bind_after_go(*args, **kwargs):
    print("IN_WINDOW", flush=True)
    sys.stdin.readline()
    return real_bind(*args, **kwargs)

broker.bind_servers = bind_after_go
"""
    + RUN_BROKER
)

# Says when it is about to take the socket lock and when it starts to reclaim
# the socket, so a test can order it against another broker.
ANNOUNCE_SCRIPT = (
    LOAD_BROKER
    + """
real_lock, real_reclaim = broker.held_flock, broker.reclaim_socket_path

def announce_lock(path, wait):
    print("ATTEMPT", flush=True)
    return real_lock(path, wait)

def announce_reclaim(path):
    print("RECLAIMING", flush=True)
    return real_reclaim(path)

broker.held_flock = announce_lock
broker.reclaim_socket_path = announce_reclaim
"""
    + RUN_BROKER
)


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSigtermDuringBind(SocketDirCase):
    def test_a_sigterm_right_after_the_bind_still_removes_the_socket(self):
        done = subprocess.run(
            self.script_argv(SIGTERM_AFTER_BIND_SCRIPT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.lexists(self.sock))
        with open(broker_log_path(self.sock)) as log:
            self.assertIn("SIGTERM; shutting down", log.read())


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSigtermDuringALogWrite(SocketDirCase):
    def test_a_sigterm_inside_the_log_write_still_ends_the_broker(self):
        child = self.spawn_broker(self.script_argv(SIGTERM_IN_LOG_WRITE_SCRIPT))
        self.assertEqual(child.stdout.readline(), "READY\n", child.stderr)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self.sock)
            c.sendall(b"GET /health HTTP/1.0\r\n\r\n")  # its log line takes the lock
        # A handler that logged would wait for the lock its own thread holds.
        self.assertEqual(child.wait(30), 0)
        self.assertFalse(os.path.lexists(self.sock))
        self.assertFalse(os.path.lexists(self.ctl))


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSocketLock(SocketDirCase):
    # reclaim (probe, then unlink) and bind are two steps; a second broker
    # between them could unlink the first one's fresh socket. The lock makes
    # the pair one step.
    def lock_is_held(self):
        fd = os.open(self.sock + ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # drops the lock when this probe got it
        return False

    def test_the_lock_is_held_from_reclaim_through_bind_and_free_after(self):
        child = self.spawn_broker(self.script_argv(PAUSE_SCRIPT), stdin=subprocess.PIPE)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline(), "IN_WINDOW\n")
        self.assertTrue(self.lock_is_held())
        child.stdin.write("go\n")
        child.stdin.flush()
        self.assertEqual(child.stdout.readline(), "READY\n")
        self.assertFalse(self.lock_is_held())

    def test_a_second_broker_cannot_reclaim_the_socket_the_first_is_about_to_bind(
        self,
    ):
        first = self.spawn_broker(self.script_argv(PAUSE_SCRIPT), stdin=subprocess.PIPE)
        self.addCleanup(first.stdin.close)
        self.assertEqual(first.stdout.readline(), "IN_WINDOW\n")
        # The first holds the lock between its reclaim and its bind, with no
        # socket on disk yet. Without the lock the second would reclaim nothing,
        # bind first, and make the first fail; with it, the second waits.
        second = self.spawn_broker(self.script_argv(ANNOUNCE_SCRIPT))
        self.assertEqual(second.stdout.readline(), "ATTEMPT\n")
        first.stdin.write("go\n")
        first.stdin.flush()
        self.assertEqual(first.stdout.readline(), "READY\n")
        self.assertEqual(second.stdout.readline(), "RECLAIMING\n")
        self.assertEqual(
            second.stdout.readline(),
            "ERROR=another aws-broker is already serving %s\n" % self.sock,
        )
        self.assertEqual(second.wait(30), 1)


class TestBindServer(SocketDirCase):
    def test_socket_is_mode_0600_and_umask_is_restored(self):
        before = os.umask(0o022)
        try:
            server, identity = broker.bind_guest_server(self.sock, {}, 1)
            self.addCleanup(server.server_close)
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(before)
        st = os.lstat(self.sock)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(identity, (st.st_dev, st.st_ino))

    def test_bind_failure_is_a_handshake_error(self):
        missing = os.path.join(self.dir, "no-such-dir", "s.sock")
        out = self.fails(broker.bind_server, missing, broker.HealthHandler)
        self.assertIn("cannot bind", out)


class ServedBroker(SocketDirCase):
    allow = ("dev", "prod")
    min_remint_interval = 0.0

    def setUp(self):
        super().setUp()
        for name, value in [
            ("log", mock.Mock()),
            ("MIN_REMINT_INTERVAL_SECONDS", self.min_remint_interval),
        ]:
            patch = mock.patch.object(broker, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.log = broker.log
        self.caches = {p: broker.ProfileCache(p) for p in self.allow}
        self.serve(broker.bind_guest_server(self.sock, self.caches, 4242)[0])

    def serve(self, server):
        """Serve on a thread until the test ends."""
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        self.addCleanup(self.stop, server, thread)

    def stop(self, server, thread):
        server.shutdown()
        thread.join()
        server.server_close()

    def exchange(self, request, half_close=False, sock=None):
        """Send raw bytes, return everything the broker answers until it closes."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(sock or self.sock)
            try:
                c.sendall(request)
                if half_close:
                    c.shutdown(socket.SHUT_WR)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the broker answered and hung up before reading it all
            raw = b""
            try:
                while chunk := c.recv(4096):
                    raw += chunk
            except ConnectionResetError:
                pass  # the broker closed with part of the request unread
        return raw

    def get(self, path, sock=None):
        raw = self.exchange(f"GET {path} HTTP/1.0\r\n\r\n".encode(), sock=sock)
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), body.decode()

    def logged(self):
        return "\n".join(str(call.args[0]) for call in self.log.call_args_list)

    def aws(self, *results):
        """Mock the aws CLI: one result forever, or each result in turn. Returns
        the mock, whose calls are the mints."""
        if len(results) == 1:
            patch = mock.patch.object(broker, "run_aws", return_value=results[0])
        else:
            patch = mock.patch.object(broker, "run_aws", side_effect=list(results))
        run = patch.start()
        self.addCleanup(patch.stop)
        return run

    def hung_mint(self):
        """Mints block until the returned `release` is set; `in_mint` says one started."""
        in_mint = threading.Event()
        release = threading.Event()

        def mint(*args, **kwargs):
            in_mint.set()
            self.assertTrue(release.wait(10))
            return aws_result(process_json())

        self.aws(aws_result(process_json())).side_effect = mint
        self.addCleanup(release.set)
        return in_mint, release

    def served_token(self, profile="dev"):
        """The SessionToken served for the profile, or the status when it is not 200."""
        status, body = self.get(f"/creds/{profile}")
        return json.loads(body)["SessionToken"] if status == 200 else status


class TestServedRequests(ServedBroker):
    def test_the_guests_health_says_the_broker_is_up_and_names_no_host_pid(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertNotIn(str(os.getpid()), body)
        self.assertNotIn("4242", body)

    def test_the_server_header_names_no_python_version(self):
        raw = self.exchange(b"GET /health HTTP/1.0\r\n\r\n")
        self.assertIn(b"\r\nServer: aws-broker\r\n", raw)
        self.assertNotIn(b"Python", raw)

    def test_a_bad_request_or_method_gets_the_generic_text_not_the_stdlib_page(self):
        for request in [b"BREW / HTTP/1.0\r\n\r\n", b"not http at all\r\n\r\n"]:
            with self.subTest(request=request):
                raw = self.exchange(request)
                head, _, body = raw.partition(b"\r\n\r\n")
                self.assertIn(int(head.split()[1]), (400, 501))
                self.assertIn(b"Content-Type: text/plain\r\n", head)
                self.assertEqual(body, broker.REQUEST_FAILED_BODY.encode())
                self.assertNotIn(b"Python", raw)

    def test_json_and_text_replies_say_what_they_are(self):
        for path, content_type in [
            ("/health", "application/json"),
            ("/creds/other", "text/plain"),
            ("/nowhere", "text/plain"),
        ]:
            with self.subTest(path=path):
                raw = self.exchange(f"GET {path} HTTP/1.0\r\n\r\n".encode())
                self.assertIn(f"Content-Type: {content_type}\r\n".encode(), raw)

    def test_unlisted_profile_is_403(self):
        for path in ["/creds/other", "/creds/", "/creds/dev?x=1", "/creds/../dev"]:
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 403)
                self.assertIn("not allowlisted", body)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_good_credentials_are_served(self):
        self.aws(aws_result(process_json()))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["Version"], 1)
        self.assertEqual(data["SessionToken"], "token-value")

    def test_naive_expiration_is_served_with_a_zone(self):
        self.aws(aws_result(process_json(Expiration="2099-01-01T00:00:00")))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["Expiration"], "2099-01-01T00:00:00Z")

    def test_null_expiration_is_a_clean_500(self):
        self.aws(aws_result(process_json(Expiration=None)))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        self.assertIn("aws sso login --profile dev", body)

    def test_missing_session_token_is_a_clean_500(self):
        data = json.loads(process_json())
        del data["SessionToken"]
        self.aws(aws_result(json.dumps(data)))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        self.assertIn("aws sso login --profile dev", body)

    def test_500_body_leaks_no_aws_detail_but_the_log_has_it(self):
        stderr = "AccessDenied for arn:aws:iam::123456789012:role/x at /Users/user/.aws/sso/cache/abc.json"
        self.aws(aws_result(returncode=255, stderr=stderr))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        for leaked in ["123456789012", "/Users/user", "AccessDenied", "arn:aws"]:
            self.assertNotIn(leaked, body)
        self.assertIn("aws sso login --profile dev", body)
        self.assertIn("broker.log", body)
        self.assertIn("123456789012", self.logged())

    def test_unexpected_exception_is_a_clean_500_and_logged(self):
        boom = RuntimeError("secret detail /Users/user/x")
        for path in ["/creds/dev", "/health"]:
            with self.subTest(path=path):
                with (
                    mock.patch.object(broker.ProfileCache, "get", side_effect=boom),
                    mock.patch.object(broker.GuestHandler, "health", side_effect=boom),
                ):
                    status, body = self.get(path)
                self.assertEqual(status, 500)
                self.assertNotIn("secret detail", body)
                self.assertNotIn("/Users/user", body)
                self.assertIn("broker.log", body)
                self.assertIn("RuntimeError", self.logged())
                self.assertEqual(self.get("/nope")[0], 404)

    def test_control_bytes_in_a_guest_path_never_reach_the_log(self):
        self.log.side_effect = REAL_LOG
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            status, _ = self.get("/creds/a\x1b[31m")
        self.assertEqual(status, 403)
        self.assertIn("/creds/a?[31m", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_control_bytes_in_a_failed_request_path_never_reach_the_log(self):
        self.log.side_effect = REAL_LOG
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with mock.patch.object(
                broker.GuestHandler, "route", side_effect=ValueError("x")
            ):
                status, _ = self.get("/creds/a\x1b[31m")
        self.assertEqual(status, 500)
        self.assertIn("request failed", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_a_long_request_path_is_clipped_in_the_log(self):
        path = "/" + "x" * 4000
        self.assertEqual(self.get(path)[0], 404)
        self.assertNotIn(path, self.logged())
        self.assertIn("more)", self.logged())

    def test_a_flood_of_requests_leaves_the_log_and_its_backup_under_the_cap(self):
        self.log.side_effect = REAL_LOG
        path = broker_log_path(self.sock)
        saved_fd = os.dup(2)
        self.addCleanup(os.close, saved_fd)
        self.addCleanup(os.dup2, saved_fd, 2)
        cap = 4096
        with (
            mock.patch.object(broker, "LOG_MAX_BYTES", cap),
            mock.patch.object(sys, "stderr", broker.CappedLog(path)),
        ):
            for n in range(200):
                self.assertEqual(self.get("/nope-%d" % n)[0], 404)
        for name in (path, path + ".1"):
            self.assertLessEqual(os.path.getsize(name), cap, name)
            self.assertEqual(stat.S_IMODE(os.stat(name).st_mode), 0o600, name)
        with open(path) as handle:
            self.assertIn("/nope-199 ", handle.read())

    def test_secrets_never_reach_the_log(self):
        self.aws(aws_result(process_json()))
        self.get("/creds/dev")
        for secret in ["s3cr3t-value", "token-value", "AKIAEXAMPLE"]:
            self.assertNotIn(secret, self.logged())

    def test_a_client_that_hangs_up_early_costs_one_line_and_no_traceback(self):
        in_mint, release = self.hung_mint()
        failed_write_logged = threading.Event()
        self.log.side_effect = lambda line: (
            failed_write_logged.set() if "client connection failed" in line else None
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self.sock)
            c.sendall(b"GET /creds/dev HTTP/1.0\r\n\r\n")
            self.assertTrue(in_mint.wait(10))
        release.set()  # the answer is written to a closed connection
        self.assertTrue(failed_write_logged.wait(10))
        self.assertEqual(self.get("/health")[0], 200)
        self.assertNotIn("Traceback", self.logged())
        self.assertEqual(self.logged().count("client connection failed"), 1)


def minted(token, seconds):
    return aws_result(process_json(SessionToken=token, Expiration=expires_in(seconds)))


class TestCredentialCache(ServedBroker):
    def test_a_valid_cache_is_served_without_minting_again(self):
        run = self.aws(minted("first", 3600), minted("second", 3600))
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(run.call_count, 1)

    def test_credentials_inside_the_ten_minute_window_are_minted_again(self):
        run = self.aws(minted("first", 300), minted("second", 3600))
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(self.served_token(), "second")
        self.assertEqual(run.call_count, 2)

    def test_credentials_just_outside_the_window_are_not_minted_again(self):
        run = self.aws(minted("first", 1200), minted("second", 3600))
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(run.call_count, 1)

    def test_the_window_is_the_remint_window_constant(self):
        run = self.aws(minted("first", 300), minted("second", 3600))
        with mock.patch.object(broker, "REMINT_WINDOW_SECONDS", 100):
            self.assertEqual(self.served_token(), "first")
            self.assertEqual(self.served_token(), "first")
        self.assertEqual(run.call_count, 1)

    def test_a_failed_remint_keeps_serving_the_cache_while_it_is_unexpired(self):
        run = self.aws(minted("first", 300), aws_result(returncode=255, stderr="boom"))
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(run.call_count, 2)

    def test_a_mint_that_returns_expired_credentials_serves_a_500(self):
        self.aws(minted("stale", -60))
        self.assertEqual(self.served_token(), 500)

    def test_the_next_request_mints_again_after_a_failed_mint(self):
        run = self.aws(aws_result(returncode=255, stderr="boom"), minted("ok", 3600))
        self.assertEqual(self.served_token(), 500)
        self.assertEqual(self.served_token(), "ok")
        self.assertEqual(run.call_count, 2)

    def test_each_profile_has_its_own_cache(self):
        run = self.aws(minted("for-dev", 3600), minted("for-prod", 3600))
        self.assertEqual(self.served_token("dev"), "for-dev")
        self.assertEqual(self.served_token("prod"), "for-prod")
        self.assertEqual(self.served_token("dev"), "for-dev")
        profiles = [call.args[0][4] for call in run.call_args_list]
        self.assertEqual(profiles, ["dev", "prod"])


class TestRemintThrottle(ServedBroker):
    min_remint_interval = 3600.0

    def test_a_remint_inside_the_throttle_interval_is_not_attempted(self):
        run = self.aws(minted("first", 300), minted("second", 3600))
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(self.served_token(), "first")
        self.assertEqual(run.call_count, 1)

    def test_a_failed_first_mint_is_not_retried_inside_the_interval(self):
        run = self.aws(aws_result(returncode=255, stderr="boom"), minted("ok", 3600))
        self.assertEqual(self.served_token(), 500)
        self.assertEqual(self.served_token(), 500)
        self.assertEqual(run.call_count, 1)


class TestMalformedRequests(ServedBroker):
    def assert_still_serving(self):
        self.assertEqual(self.get("/health")[0], 200)

    def test_an_empty_request_gets_no_answer_and_does_not_hurt_the_broker(self):
        self.assertEqual(self.exchange(b"", half_close=True), b"")
        self.assert_still_serving()

    def test_bytes_that_are_not_utf8_are_handled(self):
        for name, request in [
            ("in the path", b"GET /creds/\xff\xfe HTTP/1.0\r\n\r\n"),
            ("in a header", b"GET /health HTTP/1.0\r\nX-Bad: \xff\xfe\r\n\r\n"),
        ]:
            with self.subTest(name):
                answer = self.exchange(request)
                self.assertRegex(answer, rb"^HTTP/1\.[01] (200|403) ")
                self.assert_still_serving()


class TestRequestHeadCap(ServedBroker):
    """The request line and headers together may be MAX_REQUEST_HEAD_BYTES; a
    client that sends more is closed unanswered."""

    cap = broker.MAX_REQUEST_HEAD_BYTES

    def request_of(self, size):
        """A GET /health whose request line and headers are `size` bytes."""
        filler = size - len(b"GET /health HTTP/1.0\r\nX: \r\n\r\n")
        return b"GET /health HTTP/1.0\r\nX: " + b"a" * filler + b"\r\n\r\n"

    def test_a_head_of_exactly_the_cap_is_served(self):
        answer = self.exchange(self.request_of(self.cap))
        self.assertRegex(answer, rb"^HTTP/1\.[01] 200 ")

    def test_one_line_a_byte_over_the_cap_is_closed_unanswered(self):
        self.assertEqual(self.exchange(self.request_of(self.cap + 1)), b"")
        self.assertIn(f"over {self.cap} bytes", self.logged())
        self.assertEqual(self.get("/health")[0], 200)

    def test_many_small_headers_over_the_cap_together_are_closed_unanswered(self):
        headers = b"".join(b"X-%d: %s\r\n" % (n, b"a" * 90) for n in range(90))
        self.assertGreater(len(headers), self.cap)
        self.assertEqual(self.exchange(b"GET /health HTTP/1.0\r\n" + headers), b"")
        self.assertEqual(self.get("/health")[0], 200)


class TestStalledClient(ServedBroker):
    """The head's time budget is patched to a few milliseconds; a client that
    stops sending is dropped and the next one is served."""

    def setUp(self):
        patch = mock.patch.object(broker, "REQUEST_HEAD_SECONDS", 0.05)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_a_client_that_stops_sending_is_cut_off_at_every_point(self):
        for sent in [
            b"",
            b"GET /hea",
            b"GET /health HTTP/1.0\r\n",
            b"GET /health HTTP/1.0\r\nX: y",
        ]:
            with self.subTest(sent=sent):
                self.assertEqual(self.exchange(sent), b"")
        self.assertEqual(self.get("/health")[0], 200)
        self.assertNotIn("Traceback", self.logged())


class TestTricklingClient(ServedBroker):
    """A client that sends a byte whenever the socket would time out is not
    stalled by any per-recv measure; the head's total time still ends it."""

    def setUp(self):
        patch = mock.patch.object(broker, "REQUEST_HEAD_SECONDS", 0.3)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_a_trickler_cannot_starve_the_next_client_beyond_the_deadline(self):
        first_byte_sent = threading.Event()
        cut_off = threading.Event()

        def trickle():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.connect(self.sock)
                try:
                    while True:
                        c.sendall(b"a")
                        first_byte_sent.set()
                        # Readable means the broker closed (or answered): stop.
                        if select.select([c], [], [], 0.01)[0]:
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass
            cut_off.set()

        trickler = threading.Thread(target=trickle, daemon=True)
        trickler.start()
        self.assertTrue(first_byte_sent.wait(10))
        self.assertEqual(self.get("/health")[0], 200)
        self.assertTrue(cut_off.wait(10))
        self.assertIn("not complete", self.logged())


class TestControlSocket(ServedBroker):
    def setUp(self):
        super().setUp()
        self.serve(broker.bind_control_server(self.ctl, self.caches, 4242)[0])

    def test_health_is_the_contract_yo_reads(self):
        status, body = self.get("/health", sock=self.ctl)
        self.assertEqual(status, 200)
        # Exactly these keys: the allow list as given, the watched pid as
        # given, the broker's own pid.
        self.assertEqual(
            json.loads(body),
            {"allow": sorted(self.allow), "pid": os.getpid(), "watch_pid": 4242},
        )

    def test_credentials_are_never_served_on_it(self):
        run = self.aws(aws_result(process_json()))
        self.assertEqual(self.get("/creds/dev", sock=self.ctl)[0], 404)
        run.assert_not_called()

    def test_health_is_answered_while_the_guest_socket_is_inside_a_mint(self):
        in_mint, release = self.hung_mint()
        guest = threading.Thread(target=self.get, args=("/creds/dev",))
        guest.start()
        self.addCleanup(guest.join)
        self.addCleanup(release.set)  # before the join, which waits for the mint
        self.assertTrue(in_mint.wait(10))
        self.assertEqual(self.get("/health", sock=self.ctl)[0], 200)
        release.set()


class TestBindServers(SocketDirCase):
    def test_both_sockets_are_bound_and_both_identities_recorded(self):
        bound = broker.bind_servers(self.sock, self.ctl, {}, 1)
        self.addCleanup(bound.guest.server_close)
        self.addCleanup(bound.control.server_close)
        self.assertEqual([b.path for b in bound.sockets], [self.sock, self.ctl])
        for path, identity in bound.sockets:
            st = os.lstat(path)
            self.assertEqual(identity, (st.st_dev, st.st_ino))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_a_control_socket_that_cannot_be_bound_leaves_no_guest_socket(self):
        missing = os.path.join(self.dir, "no-such-dir", "c.sock")
        out = self.fails(broker.bind_servers, self.sock, missing, {}, 1)
        self.assertIn("cannot bind", out)
        self.assertFalse(os.path.lexists(self.sock))

    def test_unlinking_the_bound_sockets_removes_both(self):
        bound = broker.bind_servers(self.sock, self.ctl, {}, 1)
        bound.guest.server_close()
        bound.control.server_close()
        broker.unlink_bound(bound.sockets)
        self.assertFalse(os.path.lexists(self.sock))
        self.assertFalse(os.path.lexists(self.ctl))


class TestControlSocketReclaim(SocketDirCase):
    def test_a_live_control_socket_stops_a_second_broker_and_leaves_no_guest_socket(
        self,
    ):
        live, _ = broker.bind_guest_server(self.ctl, {}, 1)
        self.addCleanup(live.server_close)
        done = subprocess.run(
            broker_argv(self.sock, self.ctl), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            done.stdout,
            "ERROR=another aws-broker is already serving %s\n" % self.ctl,
        )
        self.assertFalse(os.path.lexists(self.sock))

    @unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
    def test_a_stale_control_socket_is_replaced(self):
        stale, _ = broker.bind_guest_server(self.ctl, {}, 1)
        stale.server_close()  # closed but the file remains
        self.start_broker()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(self.ctl)


class TestHandlerCrash(ServedBroker):
    def test_a_traceback_from_a_crashed_handler_is_sanitized_like_every_log_line(self):
        self.log.side_effect = REAL_LOG
        boom = RuntimeError("guest said \x1b[31m")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with mock.patch.object(
                broker.HealthHandler, "handle_one_request", side_effect=boom
            ):
                self.assertEqual(self.exchange(b"GET /health HTTP/1.0\r\n\r\n"), b"")
            self.assertEqual(self.get("/health")[0], 200)
        self.assertIn("RuntimeError: guest said ?[31m", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
