import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BROKER_PATH = str(ROOT / "aws-broker")
YO_PATH = ROOT / "yo"


def load_broker():
    loader = importlib.machinery.SourceFileLoader("aws_broker", BROKER_PATH)
    spec = importlib.util.spec_from_loader("aws_broker", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["aws_broker"] = module
    loader.exec_module(module)
    return module


broker = load_broker()
# ServedBroker replaces broker.log with a mock; these keep the real writer.
REAL_LOG = broker.log


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
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def pattern_in(text, regex):
    m = re.search(regex, text)
    if m is None:
        raise AssertionError(f"no match for {regex!r}")
    return m.group(1)


BROKER_PATTERN_RE = r'(?m)^PROFILE_NAME_PATTERN = r"([^"]+)"$'
YO_PATTERN_RE = r'(?m)^AWS_PROFILE_NAME_RE = re\.compile\(r"([^"]+)"\)$'


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

    def test_extraction_reads_the_literal_from_source_text(self):
        sample = 'X = 1\nAWS_PROFILE_NAME_RE = re.compile(r"^[a-z]+$")\n'
        self.assertEqual(pattern_in(sample, YO_PATTERN_RE), "^[a-z]+$")

    def test_yo_carries_the_same_pattern(self):
        broker_pattern = pattern_in(Path(BROKER_PATH).read_text(), BROKER_PATTERN_RE)
        yo_pattern = pattern_in(YO_PATH.read_text(), YO_PATTERN_RE)
        self.assertEqual(broker_pattern, yo_pattern)
        self.assertEqual(broker_pattern, broker.PROFILE_NAME_PATTERN)


class TestControlByteFilterMatchesYo(unittest.TestCase):
    # broker.log and yo's terminal output are read by the same person; a byte
    # one filter lets through must not be one the other replaces.
    def test_yo_and_the_broker_replace_the_same_characters(self):
        from test_yo import yo

        for code in range(0x200):
            char = chr(code)
            with self.subTest(code=hex(code)):
                self.assertEqual(
                    broker.sanitize(char), yo.TERMINAL_CONTROL_RE.sub("?", char)
                )


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

    def test_naive_expiration_is_accepted(self):
        creds, err = self.run_export(
            aws_result(process_json(Expiration="2099-01-01T00:00:00"))
        )
        self.assertIsNone(err)
        self.assertIsNotNone(creds)

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
        broker.unlink_if_same_identity(self.sock, identity)
        self.assertTrue(os.path.exists(self.sock))

    def test_does_not_follow_a_symlink_to_the_identity(self):
        real = os.path.join(self.dir, "real.sock")
        server, identity = broker.bind_server(real, {}, 1)
        server.server_close()
        os.symlink(real, self.sock)
        broker.unlink_if_same_identity(self.sock, identity)
        self.assertTrue(os.path.islink(self.sock))
        self.assertTrue(os.path.exists(real))


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

    def setUp(self):
        super().setUp()
        log_patch = mock.patch.object(broker, "log")
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)
        states = {p: broker.State(p, 0.0) for p in self.allow}
        self.server, _ = broker.bind_server(
            self.sock, states, 4242, max_concurrent=self.max_concurrent
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

    def get(self, path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(self.sock)
            c.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
            raw = b""
            while chunk := c.recv(4096):
                raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), body.decode()

    def logged(self):
        return "\n".join(str(call.args[0]) for call in self.log.call_args_list)

    def aws(self, result):
        patch = mock.patch.object(broker.subprocess, "run", return_value=result)
        patch.start()
        self.addCleanup(patch.stop)


class TestServedRequests(ServedBroker):
    def test_health_reports_pid_watch_pid_and_sorted_allow(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(sorted(data), ["allow", "pid", "watch_pid"])
        self.assertEqual(data["allow"], ["dev", "prod"])
        self.assertEqual(data["watch_pid"], 4242)
        self.assertEqual(data["pid"], os.getpid())

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

    def test_naive_expiration_is_served_not_dropped(self):
        self.aws(aws_result(process_json(Expiration="2099-01-01T00:00:00")))
        self.assertEqual(self.get("/creds/dev")[0], 200)

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
        stderr = "AccessDenied for arn:aws:iam::123456789012:role/x at /Users/xiii/.aws/sso/cache/abc.json"
        self.aws(aws_result(returncode=255, stderr=stderr))
        status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        for leaked in ["123456789012", "/Users/xiii", "AccessDenied", "arn:aws"]:
            self.assertNotIn(leaked, body)
        self.assertIn("aws sso login --profile dev", body)
        self.assertIn("broker.log", body)
        self.assertIn("123456789012", self.logged())

    def test_unexpected_exception_is_a_clean_500_and_logged(self):
        boom = RuntimeError("secret detail /Users/xiii/x")
        with mock.patch.object(broker.State, "get", side_effect=boom):
            status, body = self.get("/creds/dev")
        self.assertEqual(status, 500)
        self.assertNotIn("secret detail", body)
        self.assertNotIn("/Users/xiii", body)
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


class TestSingleSlot(ServedBroker):
    max_concurrent = 1

    def test_slot_is_released_after_each_request(self):
        for _ in range(3):
            self.assertEqual(self.get("/health")[0], 200)

    def test_slot_is_released_after_a_failed_request(self):
        with mock.patch.object(broker.Handler, "_route", side_effect=ValueError("x")):
            self.assertEqual(self.get("/health")[0], 500)
        self.assertEqual(self.get("/health")[0], 200)


class TestNoSlotFree(ServedBroker):
    max_concurrent = 0

    def test_request_is_turned_away_with_503(self):
        status, body = self.get("/health")
        self.assertEqual(status, 503)
        self.assertIn("busy", body)
        self.assertIn("503", self.logged())


if __name__ == "__main__":
    unittest.main()
