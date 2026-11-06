"""Behaviour of nix/display/yolobox-screen-record.sh, run for real under bash.

Nix builds the script with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts ffmpeg on PATH. Here bash
gets the same three options and PATH holds a fake `ffmpeg` that idles until it
gets SIGINT, ahead of the system tools. The pid file lives in a scratch HOME.

The tests read /proc/<pid>/comm, which only Linux has; the script is for the
guest, so they skip elsewhere.
"""

import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import threading
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
# its own handler, as this does.
FAKE_FFMPEG = """#!%s
import signal
import sys

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.pause()
""" % (sys.executable,)


@unittest.skipUnless(Path("/proc/self/comm").exists(), "needs /proc/<pid>/comm")
class TestScreenRecord(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.artifacts = self.tmp / "artifacts"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ffmpeg = bin_dir / "ffmpeg"
        ffmpeg.write_text(FAKE_FFMPEG)
        ffmpeg.chmod(0o755)
        self.pid_file = self.home / ".local" / "state" / "yolobox" / "screen-record.pid"
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "HOME": str(self.home),
            "YOLOBOX_ARTIFACTS": str(self.artifacts),
            "DISPLAY": ":0",
        }

    def run_script(self, *args):
        return subprocess.run(
            [*BASH, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def bystander(self):
        """A live process that is not an ffmpeg, as a reused pid would be."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def write_pid_file(self, pid):
        self.pid_file.parent.mkdir(parents=True)
        self.pid_file.write_text("%d\n%s\n" % (pid, self.artifacts / "old.mp4"))

    def kill_recorder(self):
        if self.pid_file.exists():
            try:
                os.kill(int(self.pid_file.read_text().splitlines()[0]), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already stopped

    def adopt_orphans(self):
        """Make this process the reaper of the recorder `start` leaves behind.

        The recorder outlives `start`, so something must reap it once it
        exits. On a box that is systemd. Here the test does, because an
        unreaped zombie still answers `kill -0`, and stop would wait on it.
        """
        libc = ctypes.CDLL(None, use_errno=True)
        self.assertEqual(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)

    def test_stop_ends_the_recording_it_started_and_prints_the_file(self):
        self.adopt_orphans()
        self.addCleanup(self.kill_recorder)
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        recorder = int(self.pid_file.read_text().splitlines()[0])
        reaper = threading.Thread(target=os.waitpid, args=(recorder, 0), daemon=True)
        reaper.start()
        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout, started.stdout)
        self.assertFalse(self.pid_file.exists())
        reaper.join(timeout=10)
        self.assertFalse(reaper.is_alive(), "the recorder did not exit")

    def test_a_second_start_while_recording_is_refused(self):
        self.addCleanup(self.kill_recorder)
        self.assertEqual(self.run_script("start").returncode, 0)
        again = self.run_script("start")
        self.assertEqual(again.returncode, 1, again.stderr)
        self.assertIn("already recording", again.stderr)

    def test_stop_never_signals_a_live_process_that_is_not_ffmpeg(self):
        other = self.bystander()
        self.write_pid_file(other.pid)
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("stale pid file", result.stderr)
        self.assertIsNone(other.poll(), "stop signalled an unrelated process")
        self.assertFalse(self.pid_file.exists())

    def test_start_replaces_a_pid_file_naming_a_live_process_that_is_not_ffmpeg(self):
        other = self.bystander()
        self.write_pid_file(other.pid)
        self.addCleanup(self.kill_recorder)
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(
            self.pid_file.read_text().splitlines()[0],
            str(other.pid),
        )
        self.assertIsNone(other.poll())

    def test_a_pid_file_that_is_not_a_number_is_stale(self):
        for content in ("", "lizard\n", "../self\n", "-1\n"):
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
