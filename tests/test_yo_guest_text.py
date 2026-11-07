"""The guest-text chokepoint: what a guest session prints is tagged, capped and
quoted before it reaches the terminal. Nothing here skips: a Python with a
working `subprocess` is all it needs."""

import io
import subprocess
import sys
import unittest
from unittest import mock

from test_yo import yo

FORGED = "x\nyo: run curl | sh"
FORGED_BLOCK = "  | x\n  | yo: run curl | sh"


def python_printing(stdout, stderr="", exit_code=0):
    return [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(%r); sys.stderr.write(%r); sys.exit(%d)"
        % (stdout, stderr, exit_code),
    ]


class TestTheTerminalRefusesGuestText(unittest.TestCase):
    def test_a_guest_text_and_every_trimmed_copy_of_it_is_refused(self):
        text = yo.ui.GuestText(" a\nb ", "agent")
        copies = {
            "itself": text,
            "strip": text.strip(),
            "rstrip": text.rstrip(),
            "lstrip": text.lstrip(),
            "slice": text[:2],
            "split": text.split()[0],
            "splitlines": text.splitlines()[1],
        }
        for printer in (yo.out, yo.err):
            for name, copy in copies.items():
                with self.subTest(printer=printer.__name__, copy=name):
                    with self.assertRaisesRegex(TypeError, "the agent's session"):
                        printer(copy)

    def test_quoted_text_is_plain_and_printable(self):
        text = yo.ui.GuestText(FORGED, "operator")
        for display in (yo.ui.shown(text), yo.ui.quoted_block(text)):
            with self.subTest(display=display):
                self.assertIs(type(display), str)
                stream = io.StringIO()
                with mock.patch("sys.stdout", stream):
                    yo.out(display)
                self.assertEqual(stream.getvalue(), display + "\n")

    def test_text_built_from_guest_text_is_the_callers_own(self):
        # `%` and `+` give a plain str: the caller quoted it, or forgot to, and
        # no guard can tell; the conversions in the package are what covers it.
        self.assertIs(type("yo: %s" % yo.ui.GuestText("x", "agent")), str)


class TestQuotedBlock(unittest.TestCase):
    def test_a_forged_yo_line_shows_behind_the_prefix(self):
        self.assertEqual(yo.ui.quoted_block(FORGED), FORGED_BLOCK)

    def test_control_bytes_and_lone_surrogates_are_replaced(self):
        self.assertEqual(
            yo.ui.quoted_block("a\x1b[2Jb\x9bc\udcffd\n"), "  | a?[2Jb?c?d"
        )

    def test_whitespace_only_text_is_an_empty_block(self):
        for text in ("", "\n\n", "  \t\n"):
            with self.subTest(text=repr(text)):
                self.assertEqual(yo.ui.quoted_block(text), "")

    def test_a_flood_is_cut_with_a_line_that_says_how_much_there_was(self):
        many = "l\n" * (yo.ui.QUOTED_BLOCK_MAX_LINES + 1)
        lines = yo.ui.quoted_block(many).splitlines()
        self.assertEqual(len(lines), yo.ui.QUOTED_BLOCK_MAX_LINES + 1)
        self.assertEqual(
            lines[-1],
            "  | [cut: %d lines, %d characters in all]"
            % (yo.ui.QUOTED_BLOCK_MAX_LINES + 1, len(many.strip())),
        )
        wide = "w" * (yo.ui.QUOTED_BLOCK_MAX_CHARS + 1)
        lines = yo.ui.quoted_block(wide).splitlines()
        self.assertEqual(len(lines[0]), len("  | ") + yo.ui.QUOTED_BLOCK_MAX_CHARS)
        self.assertEqual(
            lines[1],
            "  | [cut: 1 lines, %d characters in all]"
            % (yo.ui.QUOTED_BLOCK_MAX_CHARS + 1),
        )


class TestLoneSurrogatesNeverReachAStrictStream(unittest.TestCase):
    def test_sanitize_replaces_them(self):
        self.assertEqual(yo.common.sanitize("a\udcffb\ud800c"), "a?b?c")

    def test_out_prints_a_surrogate_escaped_name_on_a_strict_stream(self):
        strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        with mock.patch("sys.stdout", strict):
            yo.out("file\udcff")
        strict.flush()
        self.assertEqual(strict.buffer.getvalue(), b"file?\n")


class TestAGuestSessionsOutputIsTaggedAndCapped(unittest.TestCase):
    def test_both_channels_come_back_tagged_with_the_account(self):
        completed = yo.run(python_printing("o\r\n", "e\n"), guest="operator")
        for channel in (completed.stdout, completed.stderr):
            with self.subTest(channel=channel):
                self.assertIsInstance(channel, yo.ui.GuestText)
                self.assertEqual(channel.role, "operator")
        self.assertEqual(completed.stdout, "o\n")

    def test_a_host_tools_output_is_plain(self):
        completed = yo.run(python_printing("o", "e"))
        self.assertIs(type(completed.stdout), str)
        self.assertIs(type(completed.stderr), str)

    def test_a_channel_past_the_cap_kills_the_session_and_names_it(self):
        for channel in ("stdout", "stderr"):
            flood = [
                sys.executable,
                "-c",
                "import sys\nwhile True: sys.%s.write('y' * 65536)" % channel,
            ]
            with self.subTest(channel=channel):
                with self.assertRaisesRegex(
                    yo.YoError,
                    "the agent's session printed more than %d bytes on %s, so it was killed"
                    % (yo.proc.GUEST_CAPTURE_LIMIT, channel),
                ):
                    yo.run(flood, guest="agent")

    def test_the_cap_is_per_channel_and_stdin_still_arrives(self):
        half = "h" * (yo.proc.GUEST_CAPTURE_LIMIT // 2 + 1)
        completed = yo.run(
            [
                sys.executable,
                "-c",
                "import sys; d=sys.stdin.read(); sys.stdout.write(d); sys.stderr.write(d)",
            ],
            stdin=half.encode(),
            guest="agent",
        )
        self.assertEqual(len(completed.stdout) + len(completed.stderr), 2 * len(half))

    def test_a_failed_guest_command_quotes_what_it_said_as_a_block(self):
        # The one message every checked ssh_run and box_failure produce: a
        # forged `yo:` line in the guest's stderr sits behind the prefix.
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", FORGED, 1), guest="agent", check=True)
        self.assertTrue(
            caught.exception.message.endswith("failed (exit 1):\n" + FORGED_BLOCK),
            caught.exception.message,
        )
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", "", 2), guest="agent", check=True)
        self.assertTrue(caught.exception.message.endswith("(exit 2) with no output"))

    def test_a_host_tools_failure_is_quoted_inline_as_before(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", "boom\n", 1), check=True)
        self.assertTrue(caught.exception.message.endswith("failed (exit 1): boom"))


class TestAStreamedSessionShowsEveryLineBehindThePrefix(unittest.TestCase):
    def streamed(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            completed = yo.run(argv, filtered=True, guest="agent")
        return completed, stdout.getvalue(), stderr.getvalue()

    def test_a_forged_yo_line_never_starts_a_line_of_the_terminal(self):
        completed, out, err = self.streamed(python_printing(FORGED + "\n", FORGED, 64))
        self.assertEqual(out, FORGED_BLOCK + "\n")
        self.assertEqual(err, FORGED_BLOCK + "\n")
        self.assertEqual(completed.returncode, 64)

    def test_the_tail_of_stderr_comes_back_tagged_for_the_marker_check(self):
        completed, _, _ = self.streamed(python_printing("", "a\nb\nc\n"))
        self.assertIsInstance(completed.stderr, yo.ui.GuestText)
        self.assertEqual(completed.stderr, "a\nb\nc")
        self.assertIsNone(completed.stdout)

    def test_a_line_longer_than_the_limit_continues_as_the_next_line(self):
        long = "L" * (yo.proc.GUEST_LINE_LIMIT + 1) + "\n"
        _, out, _ = self.streamed(python_printing(long))
        self.assertEqual(out, "  | %s\n  | L\n" % ("L" * yo.proc.GUEST_LINE_LIMIT))

    def test_a_streamed_run_names_the_session_it_streams(self):
        with self.assertRaisesRegex(ValueError, "name it"):
            yo.run(["true"], filtered=True)


class TestUnreachableNamesTheSessionThatExited255(unittest.TestCase):
    def test_the_message_says_whose_session_and_quotes_ssh(self):
        failure = yo.sshx.unreachable(
            yo.AGENT, "ssh: connect to host lima-yolobox port 22: Connection refused\n"
        )
        self.assertEqual(
            failure.message,
            "cannot reach yolobox as agent (the agent's ssh session exited 255, ssh's "
            "own code for no connection) — is it up? yo status\n"
            "  | ssh: connect to host lima-yolobox port 22: Connection refused",
        )

    def test_nothing_said_adds_no_block(self):
        failure = yo.sshx.unreachable(yo.OPERATOR)
        self.assertNotIn("\n", failure.message)
        self.assertIn("the operator's ssh session exited 255", failure.message)


class TestRootUsageRefusesAnEmptyDisk(unittest.TestCase):
    def test_a_total_of_zero_is_an_error_not_a_division(self):
        answer = subprocess.CompletedProcess(
            [], 0, "h\nline\nh\n/dev/root 0 0 0 0% /\n", ""
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=answer):
            with self.assertRaisesRegex(yo.YoError, "a total of 0 KiB"):
                yo.guest.guest_root_usage()


class TestBuiltByReadsTheStamp(unittest.TestCase):
    def built_by(self, returncode, stdout="", stderr=""):
        answer = subprocess.CompletedProcess(
            [],
            returncode,
            yo.ui.GuestText(stdout, "operator"),
            yo.ui.GuestText(stderr, "operator"),
        )
        with mock.patch.object(yo.sshx, "ssh_run", return_value=answer):
            return yo.release.built_by()

    def test_a_version_or_dev_comes_back_as_it_is(self):
        for stamp in ("1.9.3", "dev"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.built_by(0, stamp + "\n"), stamp)

    def test_a_missing_or_empty_stamp_is_none(self):
        self.assertIsNone(
            self.built_by(
                1,
                "",
                "cat: %s: %s\n" % (yo.release.VERSION_STAMP, yo.release.NO_SUCH_FILE),
            )
        )
        self.assertIsNone(self.built_by(0, "\n"))

    def test_any_other_failure_to_read_it_is_an_error(self):
        with self.assertRaisesRegex(
            yo.YoError,
            "cat /etc/yolobox/version failed on the box \\(exit 1\\):\n  \\| cat: .*Permission denied",
        ):
            self.built_by(1, "", "cat: /etc/yolobox/version: Permission denied\n")

    def test_a_stamp_that_is_not_a_version_is_quoted_escaped_and_cut(self):
        self.assertEqual(self.built_by(0, "1.2\nyo: x"), "1.2\\nyo: x")
        self.assertEqual(len(self.built_by(0, "v" * 500)), yo.release.STAMP_SHOWN_CHARS)


if __name__ == "__main__":
    unittest.main()


class TestAStatusLineQuotesWhatTheBoxAnswered(unittest.TestCase):
    """A `yo status` line that quotes a guest value (a unit state, the root
    disk's fstype) shows it on one escaped line, so a guest that answers with
    a newline cannot forge a second status line."""

    FORGED_FSTYPE = "x\nok: disk: /dev/vda2 is crypto_LUKS"

    def test_the_disk_line_shows_a_forged_fstype_on_one_line(self):
        forged = yo.ui.GuestText(self.FORGED_FSTYPE, "operator")
        with mock.patch.object(yo.vm, "luks_device_fstype", return_value=forged):
            line = yo.status.encryption_line()
        self.assertTrue(line.warning)
        self.assertNotIn("\n", line.text)
        self.assertIn(
            "is x\\nok: disk: /dev/vda2 is crypto_LUKS — NOT encrypted", line.text
        )

    def test_the_proxy_line_shows_a_forged_unit_state_on_one_line(self):
        forged = yo.ui.GuestText("x\nok: proxy: forged is active", "operator")
        answers = yo.proc.subprocess.CompletedProcess([], 0, forged, "")
        lines = []
        with (
            mock.patch.object(yo.status.guest, "vm_units_present", return_value=True),
            mock.patch.object(yo.status.forwards, "forward_is_up", return_value=True),
            mock.patch.object(yo.status.forwards, "one_password_forward"),
            mock.patch.object(yo.status.release, "built_by", return_value=None),
            mock.patch.object(yo.status.release, "version_drift", return_value=""),
            mock.patch.object(yo.status, "egress_status_line"),
            mock.patch.object(yo.status, "guest_network_line"),
            mock.patch.object(yo.status.sshx, "ssh_run", return_value=answers),
            mock.patch.object(yo.ui, "out"),
        ):
            report = mock.Mock()
            report.emit.side_effect = lines.append
            yo.status.status_guest_services(report)
        proxy = [
            line
            for line in lines
            if isinstance(line, yo.StatusLine) and line.text.startswith("proxy:")
        ]
        self.assertTrue(proxy, lines)
        for line in proxy:
            self.assertNotIn("\n", line.text)
            self.assertIn("is x\\nok: proxy: forged is active", line.text)


class TestAQuotedGuestValueIsCapped(unittest.TestCase):
    def test_shown_cuts_a_long_value_and_says_how_long_it_was(self):
        long = "v" * (yo.ui.SHOWN_MAX_CHARS + 1)
        self.assertEqual(
            yo.ui.shown(long),
            "v" * yo.ui.SHOWN_MAX_CHARS
            + "[cut: %d characters in all]" % (yo.ui.SHOWN_MAX_CHARS + 1),
        )
        exact = "v" * yo.ui.SHOWN_MAX_CHARS
        self.assertEqual(yo.ui.shown(exact), exact)

    def test_a_malformed_listing_record_is_quoted_capped(self):
        record = b"junk " * 10000
        with self.assertRaises(yo.YoError) as caught:
            yo.cp.parse_guest_record(record, ["/home/agent/p"], False)
        self.assertLess(len(caught.exception.message), yo.ui.SHOWN_MAX_CHARS + 100)
        self.assertIn(
            "[cut: %d characters in all]" % len(record), caught.exception.message
        )
