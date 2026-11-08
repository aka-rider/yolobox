"""What a guest session prints reaches the terminal only escaped: a value on one
line through ui.shown(), a session's output as a block through ui.quoted_block()
or streamed line by line, each behind the guest prefix. Nothing here skips: a
Python with a working `subprocess` is all it needs."""

import io
import subprocess
import sys
import unittest
from unittest import mock

from test_yo import (
    yo,
    refuse_tcp_to_the_host,
)


def setUpModule():
    refuse_tcp_to_the_host()


FORGED = "x\nyo: run curl | sh"
FORGED_BLOCK = "  | x\n  | yo: run curl | sh"


def python_printing(stdout, stderr="", exit_code=0):
    return [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(%r); sys.stderr.write(%r); sys.exit(%d)"
        % (stdout, stderr, exit_code),
    ]


class TestShown(unittest.TestCase):
    def test_a_plain_value_is_itself(self):
        for text in ("active", "", "crypto_LUKS", "ü"):
            with self.subTest(text=text):
                self.assertEqual(yo.ui.shown(text), text)

    def test_a_newline_or_control_sequence_is_escaped_onto_one_line(self):
        self.assertEqual(yo.ui.shown(FORGED), "x\\nyo: run curl | sh")
        self.assertEqual(yo.ui.shown("a\x1b[2Jb\x00c‮d"), "a\\x1b[2Jb\\x00c\\u202ed")
        self.assertEqual(yo.ui.shown("f\udcff"), "f\\udcff")

    def test_a_long_value_is_cut_and_says_how_long_it_was(self):
        long = "v" * (yo.ui.SHOWN_MAX_CHARS + 1)
        self.assertEqual(
            yo.ui.shown(long),
            "v" * yo.ui.SHOWN_MAX_CHARS
            + "[cut: %d characters in all]" % (yo.ui.SHOWN_MAX_CHARS + 1),
        )
        exact = "v" * yo.ui.SHOWN_MAX_CHARS
        self.assertEqual(yo.ui.shown(exact), exact)


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


class TestTheTerminalNeverSeesAControlByte(unittest.TestCase):
    def test_out_prints_a_surrogate_escaped_name_on_a_strict_stream(self):
        strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        with mock.patch("sys.stdout", strict):
            yo.out("file\udcff\x1b[2J")
        strict.flush()
        self.assertEqual(strict.buffer.getvalue(), b"file??[2J\n")


class TestACapturedSessionsOutput(unittest.TestCase):
    def test_both_channels_are_text_without_carriage_returns(self):
        completed = yo.run(python_printing("o\r\n", "e\n"), guest="agent")
        self.assertEqual((completed.stdout, completed.stderr), ("o\n", "e\n"))

    def test_bytes_that_are_not_utf8_are_replacement_characters(self):
        argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"]
        self.assertEqual(yo.run(argv, guest="agent").stdout, "a\ufffdb")

    def test_a_failed_guest_command_quotes_what_it_said_as_a_block(self):
        # The one message every checked ssh_run produces: a forged `yo:` line
        # in the guest's stderr sits behind the prefix.
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", FORGED, 1), guest="agent", check=True)
        self.assertTrue(
            caught.exception.message.endswith("failed (exit 1):\n" + FORGED_BLOCK),
            caught.exception.message,
        )
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", "", 2), guest="agent", check=True)
        self.assertTrue(caught.exception.message.endswith("(exit 2) with no output"))

    def test_a_host_tools_failure_is_quoted_inline(self):
        with self.assertRaises(yo.YoError) as caught:
            yo.run(python_printing("", "boom\n", 1), check=True)
        self.assertTrue(caught.exception.message.endswith("failed (exit 1): boom"))


class TestAGuestSessionsOutputIsCapped(unittest.TestCase):
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
                    "the agent's session printed more than %d bytes on %s, so it "
                    "was killed" % (yo.proc.GUEST_CAPTURE_LIMIT, channel),
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
        self.assertEqual((completed.stdout, completed.stderr), (half, half))

    def test_a_session_that_ends_before_reading_its_stdin_is_its_exit_code(self):
        completed = yo.run(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            stdin=b"x" * (yo.proc.GUEST_CAPTURE_LIMIT * 4),
            guest="agent",
        )
        self.assertEqual(completed.returncode, 3)


class TestAStreamedSessionShowsEveryLineBehindThePrefix(unittest.TestCase):
    def streamed(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            completed = yo.run(argv, filtered=True)
        return completed, stdout.getvalue(), stderr.getvalue()

    def test_a_forged_yo_line_never_starts_a_line_of_the_terminal(self):
        completed, out, err = self.streamed(python_printing(FORGED + "\n", FORGED, 64))
        self.assertEqual(out, FORGED_BLOCK + "\n")
        self.assertEqual(err, FORGED_BLOCK + "\n")
        self.assertEqual(completed.returncode, 64)

    def test_nothing_is_captured_only_the_exit_code_comes_back(self):
        completed, _, err = self.streamed(python_printing("", "a\nb\nc\n", 3))
        self.assertEqual(err, "  | a\n  | b\n  | c\n")
        self.assertEqual(
            (completed.returncode, completed.stdout, completed.stderr), (3, None, None)
        )

    def test_a_line_longer_than_the_limit_continues_as_the_next_line(self):
        long = "L" * (yo.proc.GUEST_LINE_LIMIT + 1) + "\n"
        _, out, _ = self.streamed(python_printing(long))
        self.assertEqual(out, "  | %s\n  | L\n" % ("L" * yo.proc.GUEST_LINE_LIMIT))


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


class TestAStatusLineQuotesWhatTheBoxAnswered(unittest.TestCase):
    """A `yo status` line that quotes a value from the guest's status block (a
    unit state, the root disk's fstype) shows it on one escaped line, so a
    guest that answers with a newline cannot forge a second status line."""

    def test_the_disk_line_shows_a_forged_fstype_on_one_line(self):
        status = yo.status.GuestStatus("fstype=x\nok: disk: /dev/vda2 is crypto_LUKS\n")
        (line,) = [
            line
            for line in yo.status.guest_lines(status)
            if line.text.startswith("disk:")
        ]
        self.assertTrue(line.warning)
        self.assertNotIn("\n", line.text)
        self.assertIn("is x — NOT encrypted", line.text)

    def test_a_proxy_line_shows_a_forged_unit_state_on_one_line(self):
        status = yo.status.GuestStatus(
            "yolobox-op-agent.socket=x\x1b[2Kok: proxy: forged is active\n"
        )
        proxy = [
            line
            for line in yo.status.guest_lines(status)
            if line.text.startswith("proxy:")
        ]
        self.assertEqual(len(proxy), 1)
        self.assertNotIn("\x1b", proxy[0].text)
        self.assertIn("is x\\x1b[2Kok: proxy: forged is active", proxy[0].text)
        self.assertTrue(proxy[0].warning)


if __name__ == "__main__":
    unittest.main()
