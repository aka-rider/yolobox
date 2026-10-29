"""Behaviour of the three initrd scripts in nix/luks/, run for real under bash.

Nix builds each with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options and PATH holds a scratch directory of fake
tools ahead of the system ones. The fakes append their argv to a log file, so
the tests read what the scripts did to the disk and to systemd, not how they
did it.
"""

import os
import select
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

LUKS_DIR = Path(__file__).resolve().parent.parent / "nix" / "luks"
MIGRATE = LUKS_DIR / "yolobox-luks-migrate.sh"
UNLOCK = LUKS_DIR / "yolobox-unlock.sh"
KEYS = LUKS_DIR / "yolobox-initrd-authorized-keys.sh"

BASH = ["bash", "-o", "errexit", "-o", "nounset", "-o", "pipefail"]
MIB = 1024 * 1024
GIB = 1024 * MIB

# What the fakes report about the disk. min x 1.05 + 1 is the shrink target.
FS_MIN_BLOCKS = 1000000
FS_BLOCKS = 2000000
FS_BLOCK_SIZE = 4096
FS_TARGET = FS_MIN_BLOCKS + FS_MIN_BLOCKS // 20 + 1
# Data offset of the LUKS mapping, in 512-byte sectors (16 MiB).
MAPPER_OFFSET_SECTORS = 32768
DEVICE_BYTES = 400 * GIB
# The shrunken filesystem plus 32 MiB of header room, rounded up to 4 MiB.
REGION = -(-(FS_TARGET * FS_BLOCK_SIZE + 32 * MIB) // (4 * MIB)) * (4 * MIB)
# Where the raw plaintext tail starts: data offset plus the filesystem.
TAIL = MAPPER_OFFSET_SECTORS * 512 + FS_BLOCKS * FS_BLOCK_SIZE

DEVICE = "/dev/fake"
LABEL = "yolobox-luks"
# Not "root": a mapper name that leaked from the script would fail here.
MAPPER = "mapped"
# The private name finalize opens the partition under.
SCRATCH = MAPPER + "-finalize"
SSH_PORT = "60099"
PASSPHRASE = "k" * 64


def write_fake(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


FAKES = {
    "cryptsetup": """
echo "cryptsetup $*" >> "$FAKE_LOG"
case "$1" in
  isLuks) exit "${FAKE_ISLUKS_RC:-0}" ;;
  luksDump) cat "$FAKE_LUKSDUMP" ;;
  status) echo "  offset:  32768 sectors" ;;
  reencrypt|open) echo "stdin_bytes $(wc -c | tr -d ' ')" >> "$FAKE_LOG" ;;
esac
exit 0
""",
    "blkid": """
echo "blkid $*" >> "$FAKE_LOG"
case "$4" in
  TYPE) [ -n "${FAKE_BLKID_TYPE:-}" ] || exit 2; echo "$FAKE_BLKID_TYPE" ;;
  LABEL) [ -n "${FAKE_BLKID_LABEL:-}" ] || exit 2; echo "$FAKE_BLKID_LABEL" ;;
esac
""",
    "e2fsck": """
echo "e2fsck $*" >> "$FAKE_LOG"
exit "${FAKE_E2FSCK_RC:-1}"
""",
    "resize2fs": """
echo "resize2fs $*" >> "$FAKE_LOG"
if [ "$1" = -P ]; then
  echo "resize2fs 1.47 (fake)" >&2
  echo "Estimated minimum size of the filesystem: 1000000"
  exit 0
fi
exit "${FAKE_RESIZE2FS_RC:-0}"
""",
    "dumpe2fs": """
echo "Block count:              2000000"
echo "Block size:               4096"
""",
    "blockdev": """
echo "${FAKE_DEVICE_BYTES}"
""",
    "blkdiscard": """
echo "blkdiscard $*" >> "$FAKE_LOG"
exit "${FAKE_BLKDISCARD_RC:-0}"
""",
    "systemd-ask-password": """
echo "systemd-ask-password $*" >> "$FAKE_LOG"
printf 'k%.0s' $(seq 64)
echo
""",
}


class MigrateScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in FAKES.items():
            write_fake(bin_dir, name, body)
        self.log = self.tmp / "log"
        self.log.touch()
        self.luksdump = self.tmp / "luksdump"
        self.luksdump.write_text("LUKS header information\n")
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "FAKE_LOG": str(self.log),
            "FAKE_LUKSDUMP": str(self.luksdump),
            "FAKE_DEVICE_BYTES": str(DEVICE_BYTES),
            "FAKE_ISLUKS_RC": "1",
            "FAKE_BLKID_TYPE": "ext4",
            "YOLOBOX_SSH_LOCAL_PORT": SSH_PORT,
        }

    def run_migrate(self, *args, **env):
        args = args or (DEVICE, LABEL, MAPPER)
        return subprocess.run(
            [*BASH, str(MIGRATE), *args],
            capture_output=True,
            text=True,
            env=dict(self.env, **env),
            timeout=20,
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def luks(self, dump):
        self.luksdump.write_text(dump)
        self.env["FAKE_ISLUKS_RC"] = "0"

    def assertInOrder(self, calls, *expected):
        """Each expected line (or line prefix) appears, in this order."""
        at = -1
        for want in expected:
            later = [
                i for i, line in enumerate(calls) if i > at and line.startswith(want)
            ]
            self.assertTrue(
                later, "%r not found after line %d in %s" % (want, at, calls)
            )
            at = later[0]

    def asked_for_a_passphrase(self):
        return any(c.startswith("systemd-ask-password") for c in self.calls())


class TestMigrateEncryptsPlainExt4(MigrateScript):
    def test_ext4_is_shrunk_encrypted_finalized_and_labelled_in_that_order(self):
        result = self.run_migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertInOrder(
            calls,
            "e2fsck -f -y " + DEVICE,
            "resize2fs -P " + DEVICE,
            "resize2fs %s %d" % (DEVICE, FS_TARGET),
            "cryptsetup reencrypt",
            "cryptsetup open",
            "blkdiscard -f --offset %d" % TAIL,
            "e2fsck -f -y /dev/mapper/" + SCRATCH,
            "resize2fs /dev/mapper/" + SCRATCH,
            "cryptsetup close " + SCRATCH,
            "cryptsetup config --label %s %s" % (LABEL, DEVICE),
        )

    def test_reencrypt_covers_the_shrunken_region_at_the_default_sector_size(self):
        self.run_migrate()
        (reencrypt,) = [c for c in self.calls() if c.startswith("cryptsetup reencrypt")]
        self.assertIn("--device-size %d " % REGION, reencrypt + " ")
        self.assertIn("--reduce-device-size 32M", reencrypt)
        self.assertIn("--encrypt --type luks2", reencrypt)
        # vda2's length is not a multiple of 4096: that sector size would fail
        # after the data was already rewritten.
        self.assertNotIn("--sector-size", reencrypt)

    def test_the_discard_covers_everything_past_the_filesystem_to_the_end(self):
        self.run_migrate()
        (discard,) = [c for c in self.calls() if c.startswith("blkdiscard")]
        self.assertEqual(
            discard,
            "blkdiscard -f --offset %d --length %d %s"
            % (TAIL, DEVICE_BYTES - TAIL, DEVICE),
        )

    def test_the_passphrase_reaches_cryptsetup_without_a_trailing_newline(self):
        self.run_migrate()
        self.assertEqual(
            [c for c in self.calls() if c.startswith("stdin_bytes")],
            ["stdin_bytes 64", "stdin_bytes 64"],
        )

    def test_the_passphrase_is_asked_for_by_name_and_never_printed(self):
        result = self.run_migrate()
        (ask,) = [c for c in self.calls() if c.startswith("systemd-ask-password")]
        self.assertIn("--id=yolobox:luks-migrate", ask)
        self.assertNotIn(
            PASSPHRASE, result.stdout + result.stderr + "\n".join(self.calls())
        )

    def test_a_device_too_full_to_shrink_stops_before_touching_it(self):
        result = self.run_migrate(FAKE_DEVICE_BYTES=str(REGION + 32 * MIB - 1))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("too full", result.stderr)
        self.assertIn("systemd-cryptsetup@%s.service" % MAPPER, result.stderr)
        self.assertIn("port " + SSH_PORT, result.stderr)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith("resize2fs " + DEVICE)])
        self.assertFalse([c for c in calls if c.startswith("cryptsetup reencrypt")])

    def test_a_device_that_just_fits_is_encrypted(self):
        result = self.run_migrate(FAKE_DEVICE_BYTES=str(REGION + 32 * MIB))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_e2fsck_failure_other_than_fixed_or_reboot_advised_stops_the_run(self):
        result = self.run_migrate(FAKE_E2FSCK_RC="4")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("e2fsck on %s failed with 4" % DEVICE, result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("resize2fs")])

    def test_e2fsck_exit_0_1_and_2_all_count_as_a_clean_filesystem(self):
        for rc in ("0", "1", "2"):
            with self.subTest(e2fsck_exit=rc):
                self.assertEqual(self.run_migrate(FAKE_E2FSCK_RC=rc).returncode, 0)


class TestMigrateStates(MigrateScript):
    def test_an_unfinished_reencryption_is_resumed_then_finalized(self):
        self.luks("Requirements:  online-reencrypt-v2\n")
        result = self.run_migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertInOrder(
            calls,
            "cryptsetup reencrypt --batch-mode --progress-frequency 30 --resume-only",
            "cryptsetup open",
            "cryptsetup close " + SCRATCH,
            "cryptsetup config --label",
        )
        self.assertFalse([c for c in calls if c.startswith("resize2fs -P")])

    def test_a_labelled_disk_is_left_alone_and_nothing_is_asked(self):
        self.luks("LUKS header information\n")
        result = self.run_migrate(FAKE_BLKID_LABEL=LABEL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.asked_for_a_passphrase())
        self.assertFalse([c for c in self.calls() if c.startswith("cryptsetup open")])

    def test_a_luks_disk_without_the_label_is_finalized_only(self):
        self.luks("LUKS header information\n")
        result = self.run_migrate(FAKE_BLKID_LABEL="other")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertInOrder(
            calls,
            "cryptsetup open",
            "blkdiscard",
            "resize2fs /dev/mapper/" + SCRATCH,
            "cryptsetup close " + SCRATCH,
            "cryptsetup config --label %s %s" % (LABEL, DEVICE),
        )
        self.assertFalse([c for c in calls if c.startswith("cryptsetup reencrypt")])
        self.assertFalse([c for c in calls if c.startswith("resize2fs " + DEVICE)])

    def test_a_blank_device_is_refused(self):
        result = self.run_migrate(FAKE_BLKID_TYPE="")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("blank", result.stderr)
        self.assertFalse(self.asked_for_a_passphrase())

    def test_a_device_with_another_filesystem_is_refused_by_name(self):
        result = self.run_migrate(FAKE_BLKID_TYPE="xfs")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("xfs", result.stderr)

    def test_a_key_file_replaces_the_prompt(self):
        key_file = self.tmp / "key"
        key_file.write_text("abc")
        result = self.run_migrate(YOLOBOX_LUKS_KEY_FILE=str(key_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.asked_for_a_passphrase())
        self.assertEqual(
            [c for c in self.calls() if c.startswith("stdin_bytes")],
            ["stdin_bytes 3", "stdin_bytes 3"],
        )

    def test_an_empty_key_file_is_refused(self):
        key_file = self.tmp / "key"
        key_file.write_text("")
        result = self.run_migrate(YOLOBOX_LUKS_KEY_FILE=str(key_file))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("cryptsetup open")])

    def test_wrong_argument_count_prints_usage(self):
        for args in (("only-one",), (DEVICE, LABEL), (DEVICE, LABEL, MAPPER, "extra")):
            with self.subTest(args=args):
                result = self.run_migrate(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)
                self.assertEqual(self.calls(), [])


class TestMigrateNeverOpensTheRealMapping(MigrateScript):
    """systemd starts fsck and sysroot.mount as soon as /dev/mapper/<mapper> exists."""

    def assertOnlyTheScratchNameIsUsed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping_calls = [
            c
            for c in self.calls()
            if c.startswith(("cryptsetup open", "cryptsetup close", "cryptsetup status"))
            or "/dev/mapper/" in c
        ]
        self.assertTrue(mapping_calls)
        for call in mapping_calls:
            self.assertRegex(call, r"\b%s\b" % SCRATCH)
            self.assertNotRegex(call, r"(^|[ /])%s\b(?!-)" % MAPPER)

    def test_every_state_that_finalizes_uses_a_private_mapper_name(self):
        for name, dump, env in (
            ("encrypt", None, {}),
            ("resume", "Requirements:  online-reencrypt-v2\n", {}),
            ("finalize", "LUKS header information\n", {"FAKE_BLKID_LABEL": "other"}),
        ):
            with self.subTest(state=name):
                self.log.write_text("")
                if dump is not None:
                    self.luks(dump)
                self.assertOnlyTheScratchNameIsUsed(self.run_migrate(**env))

    def test_a_failed_finalize_has_not_opened_the_real_mapping_either(self):
        self.luks("LUKS header information\n")
        result = self.run_migrate(FAKE_BLKID_LABEL="other", FAKE_RESIZE2FS_RC="1")
        self.assertEqual(result.returncode, 5, result.stderr)
        opened = [c for c in self.calls() if c.startswith("cryptsetup open")]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].endswith(" " + SCRATCH), opened)


class TestMigrateFinalizeFailures(MigrateScript):
    def setUp(self):
        super().setUp()
        self.luks("LUKS header information\n")
        self.env["FAKE_BLKID_LABEL"] = "other"

    def assertLeftClosedAndUnlabelled(self, result):
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("left closed", result.stderr)
        calls = self.calls()
        self.assertIn("cryptsetup close " + SCRATCH, calls)
        self.assertFalse([c for c in calls if c.startswith("cryptsetup config")])

    def test_a_failing_grow_closes_the_mapping_and_does_not_label(self):
        self.assertLeftClosedAndUnlabelled(self.run_migrate(FAKE_RESIZE2FS_RC="1"))

    def test_a_failing_discard_closes_the_mapping_and_does_not_label(self):
        result = self.run_migrate(FAKE_BLKDISCARD_RC="1")
        self.assertLeftClosedAndUnlabelled(result)
        self.assertFalse([c for c in self.calls() if c.startswith("resize2fs")])

    def test_a_failing_filesystem_check_closes_the_mapping_and_does_not_label(self):
        self.assertLeftClosedAndUnlabelled(self.run_migrate(FAKE_E2FSCK_RC="8"))


class UnlockScript(unittest.TestCase):
    """yolobox-unlock against fake systemctl, journalctl and inotifywait.

    The fakes' streams are FIFOs the test writes into, so the test decides
    exactly which event reaches the helper next. The test holds each FIFO open
    read-write: no open() ever blocks, and closing it gives the fake its EOF.
    """

    ASK_ID = "cryptsetup:/dev/vda2"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.ask_dir = self.tmp / "ask"
        self.ask_dir.mkdir()
        self.reply_log = self.tmp / "reply.log"
        self.journal_tail = self.tmp / "journal-tail"
        self.journal_tail.write_text("")
        self.fifos = {}
        for name in ("journal", "inotify", "ready"):
            path = self.tmp / (name + ".fifo")
            os.mkfifo(path)
            fd = os.open(path, os.O_RDWR)
            self.addCleanup(self.close_fifo, name)
            self.fifos[name] = fd
        write_fake(
            bin_dir,
            "systemctl",
            """
for unit; do :; done
state=$(cat "$FAKE_STATE/$unit" 2>/dev/null || echo inactive)
case "$1" in
  is-failed) [ "$state" = failed ] ;;
  is-active) [ "$state" = active ] ;;
  start) echo "$unit" >> "$FAKE_START_LOG"; [ "$state" != failed ] ;;
esac
""",
        )
        # -f follows (stays open like the real one); the plain query prints a
        # scripted tail.
        write_fake(
            bin_dir,
            "journalctl",
            """
for a in "$@"; do
  if [ "$a" = -f ]; then
    exec 3< "$FAKE_JOURNAL_FIFO"
    echo journal-ready > "$FAKE_READY_FIFO"
    exec cat <&3
  fi
done
cat "$FAKE_JOURNAL_TAIL"
""",
        )
        write_fake(
            bin_dir,
            "inotifywait",
            """
exec 3< "$FAKE_INOTIFY_FIFO"
if [ -n "${FAKE_INOTIFY_COULDNT_WATCH:-}" ]; then
  echo "Couldn't watch $FAKE_INOTIFY_COULDNT_WATCH: No such file or directory" >&2
fi
echo "Setting up watches." >&2
echo "Watches established." >&2
echo inotify-ready > "$FAKE_READY_FIFO"
while IFS= read -r path <&3; do
  echo "inotify CREATE $path"
done
""",
        )
        write_fake(
            bin_dir,
            "reply-password",
            """
if [ -n "${FAKE_REPLY_FAILS_ONCE:-}" ] && [ ! -e "$FAKE_REPLY_FAILS_ONCE" ]; then
  : > "$FAKE_REPLY_FAILS_ONCE"
  exit 1
fi
body=$(cat; printf x)
body=${body%x}
printf 'reply %s %s [%s]\\n' "$1" "$2" "$body" >> "$FAKE_REPLY_LOG"
""",
        )
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "REPLY_PASSWORD": str(bin_dir / "reply-password"),
            "LUKS_DEVICE": "/dev/fake",
            "YOLOBOX_UNLOCK_UNTIL_UNIT": "systemd-cryptsetup@root.service",
            "YOLOBOX_ASK_DIR": str(self.ask_dir),
            "FAKE_STATE": str(self.state),
            "FAKE_START_LOG": str(self.tmp / "start.log"),
            "FAKE_JOURNAL_FIFO": str(self.tmp / "journal.fifo"),
            "FAKE_INOTIFY_FIFO": str(self.tmp / "inotify.fifo"),
            "FAKE_READY_FIFO": str(self.tmp / "ready.fifo"),
            "FAKE_JOURNAL_TAIL": str(self.journal_tail),
            "FAKE_REPLY_LOG": str(self.reply_log),
        }
        self.proc = None
        self.out = b""
        self.addCleanup(self.stop)

    def close_fifo(self, name):
        fd = self.fifos.pop(name, None)
        if fd is not None:
            os.close(fd)

    def start(self, stdin=PASSPHRASE + "\n", **env):
        self.proc = subprocess.Popen(
            [*BASH, str(UNLOCK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(self.env, **env),
            # The fakes are grandchildren: one group lets a failing test kill
            # them all.
            start_new_session=True,
        )
        self.proc.stdin.write(stdin.encode())
        self.proc.stdin.close()
        # communicate() flushes a non-None stdin, which is closed now.
        self.proc.stdin = None

    def stop(self):
        if self.proc is not None:
            # Always the whole group: a helper that exits but leaves a fake
            # behind must not leak it into the next test.
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()

    def set_unit(self, unit, state):
        (self.state / unit).write_text(state)

    def send(self, fifo, line):
        os.write(self.fifos[fifo], (line + "\n").encode())

    def wait_until_streams_are_up(self):
        """Both fakes have opened their FIFO and written their first lines."""
        got = b""
        while got.count(b"\n") < 2:
            got += self.read_fd(self.fifos["ready"], "the fakes to come up")

    def read_fd(self, fd, what):
        ready, _, _ = select.select([fd], [], [], 20)
        self.assertTrue(ready, "timed out waiting for " + what)
        return os.read(fd, 4096)

    def wait_for_stdout_line(self, wanted):
        """Read the helper's stdout until a line equal to `wanted`."""
        fd = self.proc.stdout.fileno()
        while wanted.encode() not in self.out.split(b"\n")[:-1]:
            chunk = self.read_fd(fd, "stdout line %r (got %r)" % (wanted, self.out))
            self.assertTrue(
                chunk, "helper closed stdout before %r: %r" % (wanted, self.out)
            )
            self.out += chunk

    def finish(self):
        out, err = self.proc.communicate(timeout=20)
        return self.proc.returncode, (self.out + out).decode(), err.decode()

    def write_ask(self, name, socket="/run/fake/reply.sock", ask_id=None):
        (self.ask_dir / name).write_text(
            "[Ask]\nPID=1\nSocket=%s\nId=%s\nMessage=Enter passphrase\n"
            % (socket, ask_id or self.ASK_ID)
        )

    def started_units(self):
        log = self.tmp / "start.log"
        return log.read_text().splitlines() if log.exists() else []

    def replies(self):
        if not self.reply_log.exists():
            return []
        return self.reply_log.read_text().splitlines()

    def sync_with_the_loop(self, marker="marker"):
        """After this returns, every earlier event has been fully handled."""
        self.send("journal", marker)
        self.wait_for_stdout_line(marker)


class TestUnlockAnswersAsks(UnlockScript):
    def test_an_ask_present_at_start_is_answered_with_exactly_the_passphrase(self):
        self.write_ask("ask.a")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertIsNone(self.proc.poll(), "exited while nothing was unlocked")
        self.assertEqual(
            self.replies(), ["reply 1 /run/fake/reply.sock [%s]" % PASSPHRASE]
        )
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.send("journal", "Finished systemd-cryptsetup@root.service")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("yolobox-unlock: answered id=%s\n" % self.ASK_ID, out)
        self.assertIn("marker\n", out)
        self.assertIn("yolobox-unlock: unlocked\n", out)

    def test_an_ask_posted_later_is_answered_when_inotify_reports_it(self):
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertEqual(self.replies(), [])
        self.write_ask("ask.b", socket="/run/fake/late.sock")
        self.send("inotify", str(self.ask_dir / "ask.b"))
        self.wait_for_stdout_line("yolobox-unlock: answered id=%s" % self.ASK_ID)
        self.assertEqual(
            self.replies(), ["reply 1 /run/fake/late.sock [%s]" % PASSPHRASE]
        )
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.send("journal", "Finished")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)

    def test_one_ask_is_answered_once_however_many_events_follow(self):
        self.write_ask("ask.a")
        self.start()
        self.wait_until_streams_are_up()
        # Journal lines, not inotify events: they are echoed, so the marker
        # proves the scans they triggered are done.
        self.send("journal", "one")
        self.send("journal", "two")
        self.sync_with_the_loop()
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.out.count(b"answered id="), 1)

    def test_each_ask_file_is_answered_separately(self):
        self.write_ask("ask.a", socket="/run/fake/a.sock")
        self.write_ask("ask.b", socket="/run/fake/b.sock")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertEqual(
            sorted(r.split()[2] for r in self.replies()),
            ["/run/fake/a.sock", "/run/fake/b.sock"],
        )

    def test_a_failed_reply_is_reported_and_retried_on_the_next_event(self):
        self.write_ask("ask.a")
        self.start(FAKE_REPLY_FAILS_ONCE=str(self.tmp / "failed-once"))
        self.wait_until_streams_are_up()
        self.send("journal", "again")
        self.wait_for_stdout_line("yolobox-unlock: answered id=%s" % self.ASK_ID)
        self.assertEqual(len(self.replies()), 1)
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.send("journal", "Finished")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0)
        self.assertIn("reply to %s failed" % self.ASK_ID, err)

    def test_an_ask_that_names_no_socket_is_reported_and_not_answered(self):
        (self.ask_dir / "ask.a").write_text("[Ask]\nId=x\n")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertEqual(self.replies(), [])
        self.set_unit("systemd-cryptsetup@root.service", "failed")
        self.send("journal", "Failed")
        rc, out, err = self.finish()
        self.assertEqual(rc, 1)
        self.assertIn("ask.a names no socket", err)


class TestUnlockOutcomes(UnlockScript):
    def test_an_already_open_disk_is_reported_unlocked_at_the_first_event(self):
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("yolobox-unlock: unlocked", out)
        self.assertEqual(self.replies(), [])

    def test_a_failed_migration_exits_2_with_its_journal_before_looking_at_the_unlock(
        self,
    ):
        self.set_unit("yolobox-luks-migrate.service", "failed")
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.journal_tail.write_text("yolobox: /dev/vda2 is too full\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 2, err)
        self.assertIn("yolobox: /dev/vda2 is too full", err)
        self.assertIn("encrypting /dev/fake failed", err)
        self.assertNotIn("unlocked", out)

    def test_a_failed_cryptsetup_unit_exits_1_naming_the_passphrase(self):
        self.set_unit("systemd-cryptsetup@root.service", "failed")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 1, err)
        self.assertIn("the passphrase does not open /dev/fake", err)
        self.assertNotIn("unlocked", out)

    def test_an_unlocked_disk_is_reported_only_once_the_root_is_mounted(self):
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.started_units(), ["sysroot.mount"])

    def test_a_root_mount_that_fails_exits_5_naming_the_unit_and_its_journal(self):
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.set_unit("sysroot.mount", "failed")
        self.journal_tail.write_text("mount: /sysroot: Can't open blockdev\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 5, err)
        self.assertIn("/dev/fake opened but sysroot.mount failed", err)
        self.assertIn("mount: /sysroot: Can't open blockdev", err)
        self.assertIn("journalctl -b", err)
        self.assertNotIn("unlocked", out)

    def test_the_root_mount_is_not_awaited_while_the_disk_is_still_closed(self):
        self.set_unit("systemd-cryptsetup@root.service", "failed")
        self.start()
        self.finish()
        self.assertEqual(self.started_units(), [])

    def test_an_empty_passphrase_is_refused_before_anything_starts(self):
        for stdin in ("", "\n"):
            with self.subTest(stdin=stdin):
                self.start(stdin=stdin)
                rc, out, err = self.finish()
                self.assertEqual(rc, 4, err)
                self.assertIn("no passphrase", err)
                self.assertEqual(self.replies(), [])

    def test_a_helper_not_told_which_unit_opens_the_disk_refuses_to_start(self):
        self.start(YOLOBOX_UNLOCK_UNTIL_UNIT="")
        rc, out, err = self.finish()
        self.assertEqual(rc, 3, err)
        self.assertIn("YOLOBOX_UNLOCK_UNTIL_UNIT", err)
        self.assertEqual(self.replies(), [])

    def test_a_watch_inotifywait_could_not_set_up_is_fatal(self):
        self.start(FAKE_INOTIFY_COULDNT_WATCH="/run/fake/ask-password")
        rc, out, err = self.finish()
        self.assertEqual(rc, 3, err)
        self.assertIn("Couldn't watch /run/fake/ask-password", err)

    def test_a_missing_ask_directory_is_created_before_it_is_watched(self):
        self.ask_dir.rmdir()
        self.start()
        self.wait_until_streams_are_up()
        self.assertTrue(self.ask_dir.is_dir())
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.send("journal", "Finished")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)

    def test_both_streams_ending_with_nothing_unlocked_exits_3(self):
        self.start()
        self.wait_until_streams_are_up()
        self.close_fifo("journal")
        self.close_fifo("inotify")
        rc, out, err = self.finish()
        self.assertEqual(rc, 3, err)
        self.assertIn("lost the ask-password watch", err)


class InitrdKeysScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        # The mountpoint is the last argument; the "ISO" is one user-data file.
        write_fake(
            bin_dir,
            "mount",
            'for last; do :; done\ncp "$FAKE_USER_DATA" "$last/user-data"\n',
        )
        write_fake(bin_dir, "umount", 'rm -f "$1/user-data"\n')
        self.user_data = self.tmp / "user-data"
        self.out = self.tmp / "keys"
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "FAKE_USER_DATA": str(self.user_data),
        }

    def run_keys(self, *args):
        return subprocess.run(
            [*BASH, str(KEYS), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=20,
        )

    def test_without_exactly_one_argument_it_prints_usage(self):
        for args in ((), ("a", "b")):
            with self.subTest(args=args):
                result = self.run_keys(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)

    @unittest.skipUnless(shutil.which("gawk"), "lima's key program needs gawk")
    def test_the_keys_under_ssh_authorized_keys_are_written_unquoted(self):
        self.user_data.write_text(
            "#cloud-config\n"
            "users:\n"
            "  - name: alice\n"
            "    ssh-authorized-keys:\n"
            '      - "ssh-ed25519 AAAA1 lima"\n'
            '      - "ssh-ed25519 AAAA2 other"\n'
            "    shell: /bin/bash\n"
        )
        result = self.run_keys(str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.out.read_text(),
            "ssh-ed25519 AAAA1 lima\nssh-ed25519 AAAA2 other\n",
        )
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o600")

    @unittest.skipUnless(shutil.which("gawk"), "lima's key program needs gawk")
    def test_user_data_without_a_key_fails_instead_of_authorizing_nobody(self):
        self.user_data.write_text("#cloud-config\nusers:\n  - name: alice\n")
        result = self.run_keys(str(self.out))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("no ssh-authorized-keys", result.stderr)


if __name__ == "__main__":
    unittest.main()
