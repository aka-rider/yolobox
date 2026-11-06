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
    """What `aws` printed, as the bytes subprocess.run hands back; text is UTF-8."""
    return subprocess.CompletedProcess(
        [],
        returncode,
        stdout=stdout.encode() if isinstance(stdout, str) else stdout,
        stderr=stderr.encode() if isinstance(stderr, str) else stderr,
    )


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
        argv = ["aws-broker", "--socket", "/x", "--watch-pid", "1", "--allow", *allow]
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
        with mock.patch.object(broker.subprocess, "run", return_value=result):
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
            with mock.patch.object(
                broker.subprocess, "run", side_effect=timed_out
            ) as run:
                creds, err = broker.export_credentials("dev")
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertIsNone(creds)
        self.assertIn("timed out after 7s", err)

    def test_a_mint_and_a_lock_wait_fit_in_the_request_deadline(self):
        self.assertLess(
            broker.PROFILE_LOCK_TIMEOUT_SECONDS + broker.MINT_TIMEOUT_SECONDS,
            broker.REQUEST_DEADLINE_SECONDS,
        )

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
        _, err = self.run_export(aws_result(process_json(Expiration="soon")))
        for secret in ["s3cr3t-value", "token-value", "AKIAEXAMPLE"]:
            self.assertNotIn(secret, err)


class TestMintOutputThatIsNotUtf8(unittest.TestCase):
    def test_a_failure_with_non_utf8_output_is_an_error_not_an_exception(self):
        with mock.patch.object(
            broker.subprocess,
            "run",
            return_value=aws_result(returncode=255, stderr=b"bad \xff\xfe byte"),
        ):
            creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("bad \ufffd\ufffd byte", err)

    def test_a_success_with_non_utf8_output_is_an_error_not_an_exception(self):
        with mock.patch.object(
            broker.subprocess, "run", return_value=aws_result(b"\xff\xfe")
        ):
            creds, err = broker.export_credentials("dev")
        self.assertIsNone(creds)
        self.assertIn("unparseable", err)

    def test_the_remint_throttle_still_counts_a_mint_that_printed_non_utf8(self):
        state = broker.State("dev", 3600.0)
        with mock.patch.object(broker, "log"):
            with mock.patch.object(
                broker.subprocess,
                "run",
                return_value=aws_result(returncode=255, stderr=b"\xff"),
            ) as run:
                self.assertIsNone(state.get())
                self.assertIsNone(state.get())
        self.assertEqual(run.call_count, 1)


class SocketDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="awsb")
        self.addCleanup(self.rmtree)
        self.sock = os.path.join(self.dir, "s.sock")

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
        server, _ = broker.bind_server(real, {}, 1)
        self.addCleanup(server.server_close)
        os.symlink(real, self.sock)
        out = self.fails(broker.reclaim_socket_path, self.sock)
        self.assertIn("not a socket", out)
        self.assertTrue(os.path.islink(self.sock))

    def test_removes_a_stale_socket(self):
        server, _ = broker.bind_server(self.sock, {}, 1)
        server.server_close()  # closed but the file remains: stale
        self.assertTrue(os.path.exists(self.sock))
        broker.reclaim_socket_path(self.sock)
        self.assertFalse(os.path.exists(self.sock))

    def test_refuses_a_live_socket(self):
        server, _ = broker.bind_server(self.sock, {}, 1)
        self.addCleanup(server.server_close)
        self.fails(broker.reclaim_socket_path, self.sock)
        self.assertTrue(os.path.exists(self.sock))


class TestUnlinkIfSameIdentity(SocketDirCase):
    def test_removes_the_file_with_that_identity(self):
        server, identity = broker.bind_server(self.sock, {}, 1)
        server.server_close()
        broker.unlink_if_same_identity(self.sock, identity)
        self.assertFalse(os.path.lexists(self.sock))

    def test_leaves_a_newer_file_alone(self):
        server, identity = broker.bind_server(self.sock, {}, 1)
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
        server, identity = broker.bind_server(self.sock, {}, 1)
        server.server_close()
        with mock.patch.object(broker.os, "lstat", side_effect=PermissionError("no")):
            with mock.patch.object(broker, "log") as log:
                broker.unlink_if_same_identity(self.sock, identity)
        self.assertIn("cannot stat", log.call_args.args[0])
        self.assertIn("no", log.call_args.args[0])
        self.assertTrue(os.path.lexists(self.sock))

    def test_a_failing_unlink_is_logged_not_ignored(self):
        server, identity = broker.bind_server(self.sock, {}, 1)
        server.server_close()
        with mock.patch.object(broker.os, "unlink", side_effect=PermissionError("no")):
            with mock.patch.object(broker, "log") as log:
                broker.unlink_if_same_identity(self.sock, identity)
        self.assertIn("cannot remove", log.call_args.args[0])
        self.assertIn("no", log.call_args.args[0])

    def test_does_not_follow_a_symlink_to_the_identity(self):
        real = os.path.join(self.dir, "real.sock")
        server, identity = broker.bind_server(real, {}, 1)
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
                broker.wait_for_pid_exit(7, "/no/such/socket", (0, 0), wait_for_exit)
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
        argv = ["aws-broker", "--socket", self.sock, "--watch-pid", str(os.getpid())]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [*argv, "--allow", "dev"]):
            with mock.patch.object(broker, "pid_exit_watch", side_effect=OSError("no")):
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                    broker.main()
        self.assertIn("ERROR=registration of a watch", out.getvalue())
        self.assertFalse(os.path.lexists(self.sock))


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSigterm(SocketDirCase):
    def test_sigterm_removes_the_socket_and_exits_cleanly(self):
        broker_proc = subprocess.Popen(
            [
                sys.executable,
                BROKER_PATH,
                "--socket",
                self.sock,
                "--watch-pid",
                str(os.getpid()),
                "--allow",
                "dev",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(broker_proc.stderr.close)
        self.addCleanup(broker_proc.stdout.close)
        self.addCleanup(broker_proc.kill)
        self.assertEqual(broker_proc.stdout.readline(), "READY\n")
        self.assertTrue(os.path.exists(self.sock))
        broker_proc.terminate()
        self.assertEqual(broker_proc.wait(30), 0, broker_proc.stderr.read())
        self.assertFalse(os.path.lexists(self.sock))


# The head of every script below: load the broker file as a module to wrap one
# step of main(), then run main() itself.
LOAD_BROKER = """
import importlib.machinery, importlib.util, os, signal, sys
loader = importlib.machinery.SourceFileLoader("aws_broker", sys.argv[1])
broker = importlib.util.module_from_spec(importlib.util.spec_from_loader("aws_broker", loader))
loader.exec_module(broker)
"""

WINDOW_SCRIPT = (
    LOAD_BROKER
    + """
real_bind = broker.bind_server

def bind_then_sigterm(*args, **kwargs):
    bound = real_bind(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGTERM)  # lands between the bind and main()'s next line
    return bound

broker.bind_server = bind_then_sigterm
sys.argv = ["aws-broker", "--socket", sys.argv[2], "--watch-pid", sys.argv[3], "--allow", "dev"]
broker.main()
"""
)


@unittest.skipUnless(HAS_PID_WATCH, "this Python has neither kqueue nor pidfd_open")
class TestSigtermDuringBind(SocketDirCase):
    def test_a_sigterm_right_after_the_bind_still_removes_the_socket(self):
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                WINDOW_SCRIPT,
                BROKER_PATH,
                self.sock,
                str(os.getpid()),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.lexists(self.sock))
        self.assertIn("SIGTERM; shutting down", done.stderr)


# A broker that stops between its reclaim and its bind until stdin says go.
PAUSE_SCRIPT = (
    LOAD_BROKER
    + """
real_bind = broker.bind_server

def bind_after_go(*args, **kwargs):
    print("IN_WINDOW", flush=True)
    sys.stdin.readline()
    return real_bind(*args, **kwargs)

broker.bind_server = bind_after_go
sys.argv = ["aws-broker", "--socket", sys.argv[2], "--watch-pid", sys.argv[3], "--allow", "dev"]
broker.main()
"""
)

# A broker that says when it is about to take the socket lock and when it starts
# to reclaim the socket, so a test can order it against another broker.
ANNOUNCE_SCRIPT = (
    LOAD_BROKER
    + """
real_lock, real_reclaim = broker.held_socket_lock, broker.reclaim_socket_path

def announce_lock(path):
    print("ATTEMPT", flush=True)
    return real_lock(path)

def announce_reclaim(path):
    print("RECLAIMING", flush=True)
    return real_reclaim(path)

broker.held_socket_lock = announce_lock
broker.reclaim_socket_path = announce_reclaim
sys.argv = ["aws-broker", "--socket", sys.argv[2], "--watch-pid", sys.argv[3], "--allow", "dev"]
broker.main()
"""
)


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

    def spawn(self, argv, **pipes):
        child = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **pipes
        )
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                self.addCleanup(stream.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def start_paused_broker(self):
        return self.spawn(
            [sys.executable, "-c", PAUSE_SCRIPT, BROKER_PATH, self.sock]
            + [str(os.getpid())],
            stdin=subprocess.PIPE,
        )

    def start_announcing_broker(self):
        return self.spawn(
            [sys.executable, "-c", ANNOUNCE_SCRIPT, BROKER_PATH, self.sock]
            + [str(os.getpid())]
        )

    def test_the_lock_is_held_from_reclaim_through_bind_and_free_after(self):
        child = self.start_paused_broker()
        self.assertEqual(child.stdout.readline(), "IN_WINDOW\n")
        self.assertTrue(self.lock_is_held())
        child.stdin.write("go\n")
        child.stdin.flush()
        self.assertEqual(child.stdout.readline(), "READY\n")
        self.assertFalse(self.lock_is_held())

    def test_a_second_broker_cannot_reclaim_the_socket_the_first_is_about_to_bind(
        self,
    ):
        first = self.start_paused_broker()
        self.assertEqual(first.stdout.readline(), "IN_WINDOW\n")
        # The first holds the lock between its reclaim and its bind, with no
        # socket on disk yet. Without the lock the second would reclaim nothing,
        # bind first, and make the first fail; with it, the second waits.
        second = self.start_announcing_broker()
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

    def test_a_lock_file_that_cannot_be_opened_is_a_handshake_error(self):
        os.mkdir(self.sock + ".lock")
        self.addCleanup(os.rmdir, self.sock + ".lock")
        # A subprocess with a timeout: a broker that ignored the lock file would
        # serve forever instead of failing.
        done = subprocess.run(
            [sys.executable, BROKER_PATH, "--socket", self.sock]
            + ["--watch-pid", str(os.getpid()), "--allow", "dev"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertTrue(done.stdout.startswith("ERROR=cannot open lock file"))


class TestPidAlive(WaitingChild):
    def test_a_running_process_is_alive(self):
        self.assertTrue(broker.pid_alive(self.child.pid))

    def test_a_process_owned_by_someone_else_is_alive(self):
        self.assertTrue(broker.pid_alive(1))

    def test_a_reaped_process_is_not_alive(self):
        self.child.stdin.close()
        self.child.wait()
        self.assertFalse(broker.pid_alive(self.child.pid))


class TestBindServer(SocketDirCase):
    def test_socket_is_mode_0600_and_umask_is_restored(self):
        before = os.umask(0o022)
        try:
            server, identity = broker.bind_server(self.sock, {}, 1)
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
        out = self.fails(broker.bind_server, missing, {}, 1)
        self.assertIn("cannot bind", out)


class ServedBroker(SocketDirCase):
    allow = ("dev", "prod")
    max_concurrent = broker.MAX_CONCURRENT_REQUESTS
    min_remint_interval = 0.0

    def setUp(self):
        super().setUp()
        log_patch = mock.patch.object(broker, "log")
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)
        self.states = {p: broker.State(p, self.min_remint_interval) for p in self.allow}
        self.server, _ = broker.bind_server(
            self.sock, self.states, 4242, max_concurrent=self.max_concurrent
        )
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        self.addCleanup(self.stop, thread)

    def stop(self, thread):
        self.server.shutdown()
        thread.join()
        self.server.server_close()

    def exchange(self, request, half_close=False):
        """Send raw bytes, return everything the broker answers until it closes."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(self.sock)
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

    def get(self, path):
        raw = self.exchange(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), body.decode()

    def logged(self):
        return "\n".join(str(call.args[0]) for call in self.log.call_args_list)

    def aws(self, *results):
        """Mock the aws CLI: one result forever, or each result in turn. Returns
        the mock, whose calls are the mints."""
        if len(results) == 1:
            patch = mock.patch.object(broker.subprocess, "run", return_value=results[0])
        else:
            patch = mock.patch.object(
                broker.subprocess, "run", side_effect=list(results)
            )
        run = patch.start()
        self.addCleanup(patch.stop)
        return run

    def served_token(self, profile="dev"):
        """The SessionToken served for the profile, or the status when it is not 200."""
        status, body = self.get(f"/creds/{profile}")
        return json.loads(body)["SessionToken"] if status == 200 else status


class TestServedRequests(ServedBroker):
    def test_health_reports_pid_watch_pid_and_sorted_allow(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(sorted(data), ["allow", "pid", "watch_pid"])
        self.assertEqual(data["allow"], ["dev", "prod"])
        self.assertEqual(data["watch_pid"], 4242)
        self.assertEqual(data["pid"], os.getpid())

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
        with mock.patch.object(broker.State, "get", side_effect=boom):
            status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        self.assertNotIn("secret detail", body)
        self.assertNotIn("/Users/user", body)
        self.assertIn("aws sso login --profile dev", body)
        self.assertIn("RuntimeError", self.logged())

    def test_unexpected_exception_off_creds_path_is_still_a_500(self):
        with mock.patch.object(broker.Handler, "_route", side_effect=ValueError("x")):
            status, body = self.get("/health")
        self.assertEqual(status, 500)
        self.assertIn("broker.log", body)

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
                broker.Handler, "_route", side_effect=ValueError("x")
            ):
                status, _ = self.get("/creds/a\x1b[31m")
        self.assertEqual(status, 500)
        self.assertIn("request failed", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_a_long_request_path_is_clipped_in_the_log(self):
        path = "/" + "x" * 20000
        self.assertEqual(self.get(path)[0], 404)
        self.assertNotIn(path, self.logged())
        self.assertIn("more)", self.logged())

    def test_secrets_never_reach_the_log(self):
        self.aws(aws_result(process_json()))
        self.get("/creds/dev")
        for secret in ["s3cr3t-value", "token-value", "AKIAEXAMPLE"]:
            self.assertNotIn(secret, self.logged())


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


class TestConcurrentClients(ServedBroker):
    clients = 8

    def test_clients_arriving_together_share_one_mint(self):
        # The first mint waits until every client is inside State.get, so the
        # other seven are queued on the profile lock when it finishes.
        arrived = 0
        arrivals = threading.Lock()
        all_arrived = threading.Event()
        real_get = broker.State.get

        def counted_get(state):
            nonlocal arrived
            with arrivals:
                arrived += 1
                if arrived == self.clients:
                    all_arrived.set()
            return real_get(state)

        def mint(*args, **kwargs):
            all_arrived.wait(10)
            return minted("shared", 3600)

        run = self.aws(minted("unused", 3600))
        run.side_effect = mint
        results = []
        with mock.patch.object(broker.State, "get", counted_get):
            threads = [
                threading.Thread(target=lambda: results.append(self.served_token()))
                for _ in range(self.clients)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(results, ["shared"] * self.clients)
        self.assertEqual(run.call_count, 1)


# http.server's own cap on a request line.
HTTP_SERVER_REQUEST_LINE_CAP = 65536


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

    def test_a_request_line_over_64_kb_is_414(self):
        answer = self.exchange(b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n")
        self.assertRegex(answer, rb"^HTTP/1\.[01] 414 ")
        self.assert_still_serving()

    def test_a_request_line_of_exactly_64_kb_is_not_414(self):
        padding = b"a" * (HTTP_SERVER_REQUEST_LINE_CAP - len(b"GET / HTTP/1.0\r\n") - 1)
        answer = self.exchange(b"GET /" + padding + b" HTTP/1.0\r\n\r\n")
        self.assertRegex(answer, rb"^HTTP/1\.[01] 404 ")


class TestSingleSlot(ServedBroker):
    max_concurrent = 1

    def test_slot_is_released_after_each_request(self):
        self.aws(aws_result(process_json()))
        for _ in range(3):
            self.assertEqual(self.get("/creds/dev")[0], 200)

    def test_slot_is_released_after_a_failed_request(self):
        self.aws(aws_result(process_json()))
        with mock.patch.object(broker.Handler, "_route", side_effect=ValueError("x")):
            self.assertEqual(self.get("/creds/dev")[0], 500)
        self.assertEqual(self.get("/creds/dev")[0], 200)

    def test_health_is_answered_while_the_only_slot_is_held(self):
        in_mint = threading.Event()
        release = threading.Event()

        def hung_mint(*args, **kwargs):
            in_mint.set()
            release.wait(10)
            return aws_result(process_json())

        run = self.aws(aws_result(process_json()))
        run.side_effect = hung_mint
        holder = threading.Thread(target=lambda: self.get("/creds/dev"))
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(in_mint.wait(10))
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["allow"], ["dev", "prod"])
        self.assertEqual(self.get("/creds/prod")[0], 503)


class TestNoSlotFree(ServedBroker):
    max_concurrent = 0

    def test_a_creds_request_is_turned_away_with_503(self):
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 503)
        self.assertIn("busy", body)
        self.assertIn("503", self.logged())

    def test_health_is_never_busy(self):
        self.assertEqual(self.get("/health")[0], 200)


class TestHandlerCrash(ServedBroker):
    def test_a_traceback_from_a_crashed_handler_is_sanitized_like_every_log_line(self):
        self.log.side_effect = REAL_LOG
        boom = RuntimeError("guest said \x1b[31m")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with mock.patch.object(
                broker.Handler, "handle_one_request", side_effect=boom
            ):
                self.assertEqual(self.exchange(b"GET /health HTTP/1.0\r\n\r\n"), b"")
        self.assertIn("RuntimeError: guest said ?[31m", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())


class TestDeadline(ServedBroker):
    """The deadline is patched to a few milliseconds; each client then blocks on
    its own recv, which returns when the broker shuts the connection down."""

    def setUp(self):
        patch = mock.patch.object(broker, "REQUEST_DEADLINE_SECONDS", 0.05)
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

    def test_a_cut_off_client_does_not_stop_the_broker(self):
        self.exchange(b"GET /hea")
        self.assertEqual(self.get("/health")[0], 200)


class TestDeadlineTimerFiringLate(ServedBroker):
    """The timer is replaced by one the test fires by hand, after the request."""

    def test_a_timer_firing_after_the_request_finished_does_nothing(self):
        timers = []

        class HandTimer:
            daemon = False

            def __init__(self, interval, function):
                timers.append(function)

            def start(self):
                pass

            def cancel(self):
                pass

        with mock.patch.object(broker.threading, "Timer", HandTimer):
            self.assertEqual(self.get("/health")[0], 200)
        (fire,) = timers
        fire()
        self.assertNotIn("deadline", self.logged())
        self.assertNotIn("closing the connection failed", self.logged())


class TestProfileLockTimeout(ServedBroker):
    def test_a_profile_whose_lock_is_held_answers_503_and_others_still_serve(self):
        self.aws(aws_result(process_json()))
        self.states["dev"].lock.acquire()
        self.addCleanup(self.states["dev"].lock.release)
        with mock.patch.object(broker, "PROFILE_LOCK_TIMEOUT_SECONDS", 0.05):
            status, body = self.get("/creds/dev")
            self.assertEqual(self.get("/creds/prod")[0], 200)
        self.assertEqual(status, 503)
        self.assertIn("busy", body)


if __name__ == "__main__":
    unittest.main()
