"""Behaviour of nix/display/yolobox-xvfb.sh, run for real under bash.

A fake `Xvfb` writes a display number to the descriptor it is given with
-displayfd, as the real one does once it accepts connections, then waits on a
fifo until it is released. A fake `systemd-notify` logs what it was asked and
releases it, so the test reads an order of events, not a delay.
"""

import os
import unittest

from shell_harness import ShellCase, REPO

SCRIPT = REPO / "nix" / "display" / "yolobox-xvfb.sh"

FAKE_XVFB = """#!/bin/sh
echo "$*" > "$FAKE_XVFB_ARGS"
fd=$(echo "$*" | sed 's/.*-displayfd \\([0-9]*\\).*/\\1/')
echo "$FAKE_XVFB_NUMBER" >&"$fd"
read -r _ < "$FAKE_XVFB_RELEASE"
"""

FAKE_NOTIFY = """#!/bin/sh
echo "$*" >> "$FAKE_NOTIFY_LOG"
echo released > "$FAKE_XVFB_RELEASE"
"""


class TestXvfbAnnouncesReadiness(ShellCase):
    def setUp(self):
        super().setUp()
        self.release = self.tmp / "release"
        os.mkfifo(self.release)
        self.notify_log = self.tmp / "notify.log"
        self.args_log = self.tmp / "args"
        self.fake("Xvfb", FAKE_XVFB)
        self.fake("systemd-notify", FAKE_NOTIFY)
        self.env.update(
            YOLOBOX_XVFB_DISPLAY=":0",
            YOLOBOX_XVFB_XKBDIR="/xkb",
            FAKE_XVFB_RELEASE=str(self.release),
            FAKE_NOTIFY_LOG=str(self.notify_log),
            FAKE_XVFB_ARGS=str(self.args_log),
        )

    def run_xvfb(self, number):
        return self.run_bash(SCRIPT, env={"FAKE_XVFB_NUMBER": number})

    def test_systemd_is_told_ready_once_xvfb_reports_the_expected_display(self):
        result = self.run_xvfb("0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.notify_log.read_text().split(), ["--ready"])
        args = self.args_log.read_text()
        self.assertIn("-nolisten tcp", args)
        self.assertIn("-xkbdir /xkb", args)

    def test_a_display_other_than_the_expected_one_is_refused_and_never_ready(self):
        for number in ("1", "", "lizard"):
            with self.subTest(number=number):
                result = self.run_xvfb(number)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not :0", result.stderr)
                self.assertFalse(self.notify_log.exists())

    def test_a_missing_setting_is_refused_by_name(self):
        for name in ("YOLOBOX_XVFB_DISPLAY", "YOLOBOX_XVFB_XKBDIR"):
            with self.subTest(name=name):
                value = self.env.pop(name)
                result = self.run_xvfb("0")
                self.env[name] = value
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)


if __name__ == "__main__":
    unittest.main()
