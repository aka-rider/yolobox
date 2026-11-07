"""Behaviour of nix/display/yolobox-screen-record.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts ffmpeg on PATH. Here bash
gets the same three options and PATH holds a fake `ffmpeg` ahead of the system
tools. Like the real one, it writes a progress block to its `-progress pipe:N`
once it records and then idles until SIGINT, or it fails first, as $FAKE_FFMPEG
tells it. The pid file lives in a scratch HOME.

The tests read /proc/<pid>/stat and the kernel's boot_id, which only Linux
has, and wait for the recorder's wrapper on a pidfd (`os.pidfd_open`); the
script is for the guest, so they skip elsewhere. Tools whose absence skips
tests: util-linux `flock` and `waitpid` (2.40 or newer).
"""

import ctypes
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "nix"
    / "display"
    / "yolobox-screen-record.sh"
)

BASH = ["bash", "-o", "errexit", "-o", "nounset", "-o", "pipefail"]

PR_SET_CHILD_SUBREAPER = 36

# Python, not sh: a background job of a non-interactive shell starts with SIGINT
# ignored, and an ignored signal cannot be trapped in sh. Real ffmpeg installs
# its own handler, as this does, and exits 255 on SIGINT.
# $FAKE_FFMPEG is `record`, `fail`, `noisy` (records, but logs an error),
# `crash` (records, then exits 3 once a line arrives on the fifo
# $FAKE_FFMPEG_CONTROL) or `await` (reads one of the others from that fifo
# first).
FAKE_FFMPEG = """#!%s
import os
import signal
import sys

signal.signal(signal.SIGINT, lambda *_: sys.exit(255))
mode = os.environ["FAKE_FFMPEG"]
if mode == "await":
    with open(os.environ["FAKE_FFMPEG_CONTROL"]) as control:
        mode = control.readline().strip()
if mode == "fail":
    sys.exit("boom: cannot open display")
url = sys.argv[sys.argv.index("-progress") + 1]
os.write(int(url[len("pipe:"):]), b"frame=0\\nprogress=continue\\n")
if mode == "noisy":
    print("x11grab: lost the display", file=sys.stderr, flush=True)
if mode == "crash":
    with open(os.environ["FAKE_FFMPEG_CONTROL"]) as control:
        control.readline()
    sys.exit(3)
signal.pause()
""" % (sys.executable,)

# Answers like util-linux waitpid when its --timeout expires.
FAKE_WAITPID_TIMES_OUT = "#!/bin/sh\nexit 3\n"


BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def starttime_of(pid):
    """Field 22 of /proc/<pid>/stat, the identity the script records."""
    stat = Path("/proc/%d/stat" % pid).read_text()
    return stat[stat.rindex(")") + 2 :].split()[19]


@unittest.skipUnless(BOOT_ID.exists(), "needs /proc/<pid>/stat and boot_id")
@unittest.skipUnless(hasattr(os, "pidfd_open"), "needs os.pidfd_open")
@unittest.skipUnless(shutil.which("flock"), "needs util-linux flock")
class TestScreenRecord(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.artifacts = self.tmp / "artifacts"
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.fake("ffmpeg", FAKE_FFMPEG)
        self.pid_file = self.home / ".local" / "state" / "yolobox" / "screen-record.pid"
        self.control = self.tmp / "control"
        self.env = {
            "PATH": os.pathsep.join(
                [str(self.bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "HOME": str(self.home),
            "YOLOBOX_ARTIFACTS": str(self.artifacts),
            "DISPLAY": ":0",
            "FAKE_FFMPEG": "record",
            "FAKE_FFMPEG_CONTROL": str(self.control),
        }

    def fake(self, name, text):
        tool = self.bin_dir / name
        tool.write_text(text)
        tool.chmod(0o755)

    def run_script(self, *args):
        return subprocess.run(
            [*BASH, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def assert_start_reports_the_failure(self, started):
        self.assertEqual(started.returncode, 1, started.stderr)
        self.assertIn("before it started recording", started.stderr)
        self.assertIn("boom: cannot open display", started.stderr)
        self.assertEqual(started.stdout, "")
        self.assertFalse(self.pid_file.exists())

    def bystander(self):
        """A live process that is not an ffmpeg, as a reused pid would be."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def write_pid_file(self, pid, start=None, boot=None):
        """A pid file for `pid`, by default with its true identity."""
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(
            "pid=%d\nstart=%s\nboot=%s\nwrapper=%d\nout=%s\n"
            % (
                pid,
                starttime_of(pid) if start is None else start,
                BOOT_ID.read_text().strip() if boot is None else boot,
                pid,
                self.artifacts / "old.mp4",
            )
        )

    def pid_file_field(self, key):
        for line in self.pid_file.read_text().splitlines():
            if line.startswith(key + "="):
                return line[len(key) + 1 :]
        self.fail("no %s= line in the pid file:\n%s" % (key, self.pid_file.read_text()))

    def kill_recorder(self):
        """End the recording `start` left behind, wrapper included.

        The wrapper writes ffmpeg's status into the state directory once
        ffmpeg is gone, so the scratch HOME must outlive the wrapper: a file
        appearing in a directory that rmtree is emptying ends the cleanup
        with ENOTEMPTY.
        """
        if not self.pid_file.exists():
            return
        try:
            os.kill(int(self.pid_file_field("pid")), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already stopped
        self.await_exit(int(self.pid_file_field("wrapper")))

    def await_exit(self, pid):
        """Block until `pid`, which is nobody's child here, has exited."""
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return  # exited and reaped
        try:
            select.select([fd], [], [])
        finally:
            os.close(fd)

    def adopt_orphans(self):
        """Make this process the reaper of the recorder `start` leaves behind.

        The recorder outlives `start`, so something must reap it once it
        exits. On a box that is systemd. Here the test does, so that the
        recorder stays a zombie the test can find once it exited.
        """
        libc = ctypes.CDLL(None, use_errno=True)
        self.assertEqual(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)

    @unittest.skipUnless(shutil.which("waitpid"), "needs util-linux waitpid")
    def test_stop_ends_the_recording_it_started_and_prints_the_file(self):
        self.adopt_orphans()
        self.addCleanup(self.kill_recorder)
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        wrapper = int(self.pid_file_field("wrapper"))
        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout, started.stdout)
        self.assertEqual(stopped.stderr, "")
        self.assertFalse(self.pid_file.exists())
        self.assertEqual(
            os.waitpid(wrapper, os.WNOHANG)[0],
            wrapper,
            "stop returned before the recorder's wrapper exited",
        )

    @unittest.skipUnless(shutil.which("waitpid"), "needs util-linux waitpid")
    def test_stop_fails_when_ffmpeg_logged_an_error_and_still_prints_the_file(self):
        self.addCleanup(self.kill_recorder)
        self.env["FAKE_FFMPEG"] = "noisy"
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        self.assertEqual(stopped.stdout, started.stdout)
        self.assertIn("may be incomplete", stopped.stderr)
        self.assertIn("x11grab: lost the display", stopped.stderr)
        self.assertFalse(self.pid_file.exists())

    @unittest.skipUnless(shutil.which("waitpid"), "needs util-linux waitpid")
    def test_stop_reports_a_recording_that_died_on_its_own_with_its_status(self):
        os.mkfifo(self.control)
        self.env["FAKE_FFMPEG"] = "crash"
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        # Opening a fifo for writing waits for its reader: the fake is running.
        with open(self.control, "w") as control:
            control.write("die\n")
        status_file = self.pid_file.with_name("screen-record.status")
        # The wrapper writes the status once it has reaped ffmpeg, then exits;
        # it is nobody's child here, so waitpid is how to wait for it.
        subprocess.run(
            ["waitpid", "--exited", "--timeout", "20", self.pid_file_field("wrapper")],
            check=True,
        )
        self.assertEqual(status_file.read_text(), "3\n")
        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        self.assertEqual(stopped.stdout, started.stdout)
        self.assertIn("already ended on its own", stopped.stderr)
        self.assertIn("status 3", stopped.stderr)
        self.assertFalse(self.pid_file.exists())
        self.assertFalse(status_file.exists())

    def test_start_fails_at_once_when_the_artifacts_directory_is_read_only(self):
        """It used to hang: ffmpeg died at its redirections before opening the
        progress fifo, and start waited on the fifo forever."""
        self.artifacts.mkdir()
        self.artifacts.chmod(0o555)
        self.addCleanup(self.artifacts.chmod, 0o755)
        started = self.run_script("start")
        self.assertEqual(started.returncode, 1, started.stderr)
        self.assertIn("cannot write to %s" % self.artifacts, started.stderr)
        self.assertEqual(started.stdout, "")
        self.assertFalse(self.pid_file.exists())

    def test_a_recording_started_in_the_same_second_as_another_gets_its_own_name(
        self,
    ):
        self.addCleanup(self.kill_recorder)
        self.artifacts.mkdir()
        now = int(time.time())
        taken = [
            self.artifacts
            / time.strftime("screen-%Y%m%d-%H%M%S.mp4", time.localtime(now + delta))
            for delta in (0, 1, 2)
        ]
        for path in taken:
            path.write_text("")
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        out = Path(started.stdout.strip())
        self.assertNotIn(out, taken)
        self.assertRegex(out.name, r"^screen-\d{8}-\d{6}-2\.mp4$")

    def test_start_reports_an_ffmpeg_that_fails_at_once_and_records_nothing(self):
        self.env["FAKE_FFMPEG"] = "fail"
        self.assert_start_reports_the_failure(self.run_script("start"))

    def test_start_waits_for_an_ffmpeg_that_fails_before_it_records(self):
        """However long ffmpeg takes to fail, start waits for its verdict."""
        os.mkfifo(self.control)
        self.env["FAKE_FFMPEG"] = "await"
        start = subprocess.Popen(
            [*BASH, str(SCRIPT), "start"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(start.kill)
        # Opening a fifo for writing waits for its reader: the fake is running.
        with open(self.control, "w") as control:
            control.write("fail\n")
        stdout, stderr = start.communicate(timeout=30)
        self.assert_start_reports_the_failure(
            subprocess.CompletedProcess(start.args, start.returncode, stdout, stderr)
        )

    def test_stop_that_times_out_says_so_and_keeps_the_pid_file(self):
        self.addCleanup(self.kill_recorder)
        self.assertEqual(self.run_script("start").returncode, 0)
        self.fake("waitpid", FAKE_WAITPID_TIMES_OUT)
        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        self.assertRegex(stopped.stderr, r"ffmpeg did not exit within \d+s")
        self.assertTrue(self.pid_file.exists())

    def test_a_second_start_while_recording_is_refused(self):
        self.addCleanup(self.kill_recorder)
        self.assertEqual(self.run_script("start").returncode, 0)
        again = self.run_script("start")
        self.assertEqual(again.returncode, 1, again.stderr)
        self.assertIn("already recording", again.stderr)

    def test_stop_never_signals_a_live_process_that_reused_the_recorders_pid(self):
        """Same pid, other start time: the kernel handed the pid on."""
        other = self.bystander()
        self.write_pid_file(other.pid, start=str(int(starttime_of(other.pid)) - 1))
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("stale pid file", result.stderr)
        self.assertIsNone(other.poll(), "stop signalled an unrelated process")
        self.assertFalse(self.pid_file.exists())

    def test_a_pid_file_from_before_a_reboot_is_stale_whatever_its_pid_runs(self):
        other = self.bystander()
        self.write_pid_file(other.pid, boot="00000000-0000-0000-0000-000000000000")
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("stale pid file", result.stderr)
        self.assertIsNone(other.poll(), "stop signalled an unrelated process")

    def test_start_replaces_a_pid_file_naming_a_process_that_reused_the_pid(self):
        other = self.bystander()
        self.write_pid_file(other.pid, start=str(int(starttime_of(other.pid)) - 1))
        self.addCleanup(self.kill_recorder)
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.pid_file_field("pid"), str(other.pid))
        self.assertIsNone(other.poll())

    def test_a_pid_file_that_is_not_a_number_is_stale(self):
        for content in ("", "lizard\n", "pid=../self\n", "pid=-1\n", "pid=\n"):
            with self.subTest(pid_file=content):
                self.pid_file.parent.mkdir(parents=True, exist_ok=True)
                self.pid_file.write_text(content)
                result = self.run_script("stop")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("stale pid file", result.stderr)
                self.assertFalse(self.pid_file.exists())

    def test_stop_without_a_pid_file_says_so(self):
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no pid file", result.stderr)

    def test_an_unknown_subcommand_prints_usage(self):
        result = self.run_script("pause")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
