"""Behaviour of the three initrd scripts in nix/luks/, run for real under bash.

Each runs under shell_harness.ShellCase with fake tools ahead of the system
ones. The fakes append their argv to a log file, so the tests read what the
scripts did to the disk and to systemd, not how they did it.

Tools whose absence skips tests: gawk (the two tests of lima's key program).
"""

import os
import select
import shutil
import signal
import subprocess
import unittest

from shell_harness import BASH, FACTS, REPO, ShellCase

LUKS_DIR = REPO / "nix" / "luks"
ENCRYPT = LUKS_DIR / "yolobox-luks-encrypt.sh"
UNLOCK = LUKS_DIR / "yolobox-unlock.sh"
KEYS = LUKS_DIR / "yolobox-initrd-authorized-keys.sh"


def exit_code(name):
    """The exit code facts.json gives the unlock helper's verdict `name`."""
    return FACTS["unlock_exit_" + name]


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
# The private name the partition is opened under to grow the filesystem.
SCRATCH = MAPPER + "-finalize"
PASSPHRASE = "k" * 64
# luksDump output of a finished disk: the label is the last thing written.
DUMP_FINISHED = "LUKS header information\nLabel:          %s\n" % LABEL
# LUKS disks that are not finished: someone else's, and an unlabelled one.
DUMPS_NOT_OURS = (
    "LUKS header information\nLabel:          other\n",
    "LUKS header information\n",
)
RECREATE = "yo destroy --yes && yo bootstrap"


FAKES = {
    "cryptsetup": """
echo "cryptsetup $*" >> "$FAKE_LOG"
case "$1" in
  isLuks) exit "${FAKE_ISLUKS_RC:-0}" ;;
  luksDump) cat "$FAKE_LUKSDUMP" ;;
  status) echo "${FAKE_STATUS_OFFSET-  offset:  32768 sectors}" ;;
  reencrypt) echo "stdin_bytes $(wc -c | tr -d ' ')" >> "$FAKE_LOG"; exit "${FAKE_REENCRYPT_RC:-0}" ;;
  open) echo "stdin_bytes $(wc -c | tr -d ' ')" >> "$FAKE_LOG" ;;
  close) exit "${FAKE_CLOSE_RC:-0}" ;;
esac
exit 0
""",
    "blkid": """
echo "blkid $*" >> "$FAKE_LOG"
[ -n "${FAKE_BLKID_TYPE:-}" ] || exit 2
echo "$FAKE_BLKID_TYPE"
""",
    "e2fsck": """
echo "e2fsck $*" >> "$FAKE_LOG"
case "$*" in */dev/mapper/*) exit "${FAKE_E2FSCK_MAPPER_RC:-1}" ;; esac
exit "${FAKE_E2FSCK_RC:-1}"
""",
    "resize2fs": """
echo "resize2fs $*" >> "$FAKE_LOG"
if [ "$1" = -P ]; then
  echo "resize2fs 1.47 (fake)" >&2
  echo "Estimated minimum size of the filesystem: ${FAKE_FS_MIN-1000000}"
  exit 0
fi
case "$*" in */dev/mapper/*) exit "${FAKE_RESIZE2FS_MAPPER_RC:-0}" ;; esac
exit "${FAKE_RESIZE2FS_RC:-0}"
""",
    "dumpe2fs": """
[ -z "${FAKE_DUMPE2FS_ERR:-}" ] || echo "$FAKE_DUMPE2FS_ERR" >&2
echo "Block count:              ${FAKE_BLOCK_COUNT:-2000000}"
[ "${FAKE_BLOCK_SIZE-4096}" = omit ] || echo "Block size:               ${FAKE_BLOCK_SIZE-4096}"
""",
    "blockdev": """
[ "${FAKE_DEVICE_BYTES}" = omit ] || echo "${FAKE_DEVICE_BYTES}"
""",
    "blkdiscard": """
echo "blkdiscard $*" >> "$FAKE_LOG"
exit "${FAKE_BLKDISCARD_RC:-0}"
""",
    "systemd-ask-password": """
echo "systemd-ask-password $*" >> "$FAKE_LOG"
[ -n "${FAKE_ASK_EMPTY:-}" ] || printf 'k%.0s' $(seq 64)
echo
""",
}


class EncryptScript(ShellCase):
    def setUp(self):
        super().setUp()
        for name, body in FAKES.items():
            self.fake(name, "#!/bin/sh" + body)
        self.log = self.tmp / "log"
        self.log.touch()
        self.luksdump = self.tmp / "luksdump"
        self.luksdump.write_text("LUKS header information\n")
        # /sys/class/block/<dev>/../queue, with the offload on unless a test says not.
        self.sysfs = self.tmp / "sysfs"
        (self.sysfs / "fake").mkdir(parents=True)
        (self.sysfs / "queue").mkdir()
        self.set_write_zeroes_max("33554432")
        self.env.update(
            FAKE_LOG=str(self.log),
            FAKE_LUKSDUMP=str(self.luksdump),
            FAKE_DEVICE_BYTES=str(DEVICE_BYTES),
            FAKE_ISLUKS_RC="1",
            FAKE_BLKID_TYPE="ext4",
            YOLOBOX_SYSFS_BLOCK=str(self.sysfs),
        )

    def run_encrypt(self, *args, **env):
        return self.run_bash(ENCRYPT, *(args or (DEVICE, LABEL, MAPPER)), env=env)

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

    def assertUntouched(self):
        """Nothing was asked and nothing was written to the device."""
        self.assertFalse(self.asks())
        written = (
            "resize2fs " + DEVICE,
            "cryptsetup reencrypt",
            "cryptsetup open",
            "cryptsetup config",
            "blkdiscard",
        )
        self.assertFalse([c for c in self.calls() if c.startswith(written)])

    def set_write_zeroes_max(self, text):
        (self.sysfs / "queue" / "write_zeroes_max_bytes").write_text(text)

    def asks(self):
        return [c for c in self.calls() if c.startswith("systemd-ask-password")]

    def discards(self):
        return [c for c in self.calls() if c.startswith("blkdiscard")]


class TestEncryptEncryptsPlainExt4(EncryptScript):
    def test_ext4_is_shrunk_encrypted_grown_and_labelled_in_that_order(self):
        result = self.run_encrypt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertInOrder(
            self.calls(),
            "e2fsck -f -y " + DEVICE,
            "resize2fs -P " + DEVICE,
            "resize2fs %s %d" % (DEVICE, FS_TARGET),
            "cryptsetup reencrypt",
            "cryptsetup open",
            "blkdiscard -z -f --offset %d" % TAIL,
            "blkdiscard -f --offset %d" % TAIL,
            "e2fsck -f -y /dev/mapper/" + SCRATCH,
            "resize2fs /dev/mapper/" + SCRATCH,
            "cryptsetup close " + SCRATCH,
            "cryptsetup config --label %s %s" % (LABEL, DEVICE),
        )
        self.assertNotIn("failed", result.stderr)

    def test_reencrypt_covers_the_shrunken_region_at_the_default_sector_size(self):
        self.run_encrypt()
        (reencrypt,) = [c for c in self.calls() if c.startswith("cryptsetup reencrypt")]
        self.assertIn("--device-size %d " % REGION, reencrypt + " ")
        self.assertIn("--reduce-device-size 32M", reencrypt)
        self.assertIn("--encrypt --type luks2", reencrypt)
        # vda2's length is not a multiple of 4096: that sector size would fail
        # after the data was already rewritten.
        self.assertNotIn("--sector-size", reencrypt)

    def test_the_tail_past_the_filesystem_is_zeroed_then_discarded_to_the_end(self):
        # Discard is advisory (qemu's default ignores it), so the plaintext is
        # overwritten first; the discard after it gives the image its holes.
        result = self.run_encrypt()
        extent = "--offset %d --length %d %s" % (TAIL, DEVICE_BYTES - TAIL, DEVICE)
        self.assertEqual(
            self.discards(), ["blkdiscard -z -f " + extent, "blkdiscard -f " + extent]
        )
        self.assertNotIn("offloads no write-zeroes", result.stdout)

    def test_without_a_write_zeroes_offload_the_tail_is_only_discarded_and_it_says_so(
        self,
    ):
        # Zeroing by hand would grow the image file by the whole tail.
        for value in ("0", "lizard", "", "-1"):
            with self.subTest(write_zeroes_max_bytes=value):
                self.log.write_text("")
                self.set_write_zeroes_max(value)
                result = self.run_encrypt()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.discards()), 1)
                self.assertNotIn("-z", self.discards()[0])
                self.assertIn("offloads no write-zeroes", result.stdout)
        (self.sysfs / "queue" / "write_zeroes_max_bytes").unlink()
        self.log.write_text("")
        result = self.run_encrypt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.discards()), 1)
        self.assertIn("offloads no write-zeroes (unknown)", result.stdout)

    def test_the_passphrase_reaches_cryptsetup_without_a_trailing_newline(self):
        self.run_encrypt()
        self.assertEqual(
            [c for c in self.calls() if c.startswith("stdin_bytes")],
            ["stdin_bytes 64", "stdin_bytes 64"],
        )

    def test_the_passphrase_is_asked_for_by_name_and_never_printed(self):
        result = self.run_encrypt()
        (ask,) = self.asks()
        self.assertIn("--id=yolobox:luks-encrypt", ask)
        self.assertNotIn(
            PASSPHRASE, result.stdout + result.stderr + "\n".join(self.calls())
        )

    def test_a_device_too_full_to_shrink_stops_before_touching_it(self):
        result = self.run_encrypt(FAKE_DEVICE_BYTES=str(REGION + 32 * MIB - 1))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("too full", result.stderr)
        self.assertIn("%s is still plaintext" % DEVICE, result.stderr)
        self.assertIn(RECREATE, result.stderr)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith("resize2fs " + DEVICE)])
        self.assertFalse([c for c in calls if c.startswith("cryptsetup reencrypt")])

    def test_a_device_that_just_fits_is_encrypted(self):
        result = self.run_encrypt(FAKE_DEVICE_BYTES=str(REGION + 32 * MIB))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_e2fsck_failure_other_than_fixed_or_reboot_advised_stops_the_run(self):
        result = self.run_encrypt(FAKE_E2FSCK_RC="4")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("e2fsck on %s failed with 4" % DEVICE, result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("resize2fs")])

    def test_e2fsck_exit_0_1_and_2_all_count_as_a_clean_filesystem(self):
        for rc in ("0", "1", "2"):
            with self.subTest(e2fsck_exit=rc):
                self.assertEqual(self.run_encrypt(FAKE_E2FSCK_RC=rc).returncode, 0)

    def test_an_empty_answer_is_refused(self):
        result = self.run_encrypt(FAKE_ASK_EMPTY="1")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no passphrase given", result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("cryptsetup open")])

    def test_every_failure_says_the_box_must_be_recreated(self):
        for name, env in (
            ("fsck", {"FAKE_E2FSCK_RC": "4"}),
            ("reencrypt", {"FAKE_REENCRYPT_RC": "1"}),
            ("discard", {"FAKE_BLKDISCARD_RC": "1"}),
            ("grow", {"FAKE_RESIZE2FS_MAPPER_RC": "1"}),
            ("close", {"FAKE_CLOSE_RC": "1"}),
        ):
            with self.subTest(failure=name):
                result = self.run_encrypt(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r"failed \(exit \d+\); .*%s" % RECREATE)


class TestEncryptTouchesOnlyAPlainOrFinishedDisk(EncryptScript):
    def test_a_labelled_disk_is_left_alone_and_nothing_is_asked(self):
        self.luks(DUMP_FINISHED)
        result = self.run_encrypt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertUntouched()

    def test_a_luks_disk_without_the_label_is_refused_with_the_one_remedy(self):
        # An interrupted encryption or someone else's disk: either way, touching
        # it could destroy what it holds, and a box is cheap to recreate.
        for dump in DUMPS_NOT_OURS:
            with self.subTest(dump=dump):
                self.log.write_text("")
                self.luks(dump)
                result = self.run_encrypt()
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("unexpected state", result.stderr)
                self.assertIn(RECREATE, result.stderr)
                self.assertUntouched()

    def test_a_blank_device_or_another_filesystem_is_refused_by_name(self):
        for fstype, word in (("", "blank"), ("xfs", "xfs")):
            with self.subTest(fstype=fstype):
                result = self.run_encrypt(FAKE_BLKID_TYPE=fstype)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(word, result.stderr)
                self.assertIn("unexpected state", result.stderr)
                self.assertIn(RECREATE, result.stderr)
                self.assertUntouched()

    def test_wrong_argument_count_prints_usage(self):
        for args in (("only-one",), (DEVICE, LABEL), (DEVICE, LABEL, MAPPER, "extra")):
            with self.subTest(args=args):
                result = self.run_encrypt(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)
                self.assertEqual(self.calls(), [])


class TestEncryptNeverOpensTheRealMapping(EncryptScript):
    """systemd starts fsck and sysroot.mount as soon as /dev/mapper/<mapper> exists."""

    def test_the_grow_uses_a_private_mapper_name(self):
        result = self.run_encrypt()
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping_calls = [
            c
            for c in self.calls()
            if c.startswith(
                ("cryptsetup open", "cryptsetup close", "cryptsetup status")
            )
            or "/dev/mapper/" in c
        ]
        self.assertTrue(mapping_calls)
        for call in mapping_calls:
            self.assertRegex(call, r"\b%s\b" % SCRATCH)
            self.assertNotRegex(call, r"(^|[ /])%s\b(?!-)" % MAPPER)

    def test_a_failed_grow_has_not_opened_the_real_mapping_either(self):
        result = self.run_encrypt(FAKE_RESIZE2FS_MAPPER_RC="1")
        self.assertEqual(result.returncode, 5, result.stderr)
        opened = [c for c in self.calls() if c.startswith("cryptsetup open")]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].endswith(" " + SCRATCH), opened)


class TestEncryptRefusesToDiscardOnUnreadableNumbers(EncryptScript):
    """bash reads "" as 0, which would discard the whole device from byte 0."""

    def assertNothingDiscarded(self, result, name):
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertRegex(result.stderr, r"%s is '[^']*', not a positive integer" % name)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith("blkdiscard")])
        self.assertIn("cryptsetup close " + SCRATCH, calls)
        self.assertFalse([c for c in calls if c.startswith("cryptsetup config")])

    def test_an_offset_cryptsetup_does_not_print_stops_before_the_discard(self):
        for output in ("", "  offset:  lizard sectors", "  offset:  0 sectors"):
            with self.subTest(status=output):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_STATUS_OFFSET=output)
                self.assertNothingDiscarded(result, "offset_sectors")

    def test_a_filesystem_dumpe2fs_reports_as_empty_stops_before_the_discard(self):
        for count in ("0", "-5", "x"):
            with self.subTest(block_count=count):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_BLOCK_COUNT=count)
                self.assertNothingDiscarded(result, "filesystem block count")


class TestEncryptRefusesToEncryptOnUnreadableNumbers(EncryptScript):
    """bash reads "" as 0, which would collapse the region reencrypt is limited to."""

    def assertNothingShrunkOrEncrypted(self, result, name):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertRegex(result.stderr, r"%s is '[^']*', not a positive integer" % name)
        self.assertIn("was not shrunk or encrypted", result.stderr)
        self.assertNotIn("discarded", result.stderr)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith("resize2fs " + DEVICE)])
        self.assertFalse(
            [
                c
                for c in calls
                if c.startswith(("cryptsetup reencrypt", "cryptsetup open"))
            ]
        )

    def test_what_dumpe2fs_says_about_a_failure_reaches_the_journal(self):
        result = self.run_encrypt(
            FAKE_BLOCK_SIZE="omit", FAKE_DUMPE2FS_ERR="Bad magic number in super-block"
        )
        self.assertIn("Bad magic number in super-block", result.stderr)

    def test_a_block_size_dumpe2fs_does_not_report_stops_before_resize2fs(self):
        for size in ("omit", "0", "x"):
            with self.subTest(block_size=size):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_BLOCK_SIZE=size)
                self.assertNothingShrunkOrEncrypted(result, "filesystem block size")

    def test_a_minimum_size_resize2fs_does_not_report_stops_before_resize2fs(self):
        for size in ("", "lizard", "0"):
            with self.subTest(minimum=size):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_FS_MIN=size)
                self.assertNothingShrunkOrEncrypted(result, "filesystem minimum size")

    def test_a_device_size_blockdev_does_not_report_stops_before_resize2fs(self):
        for size in ("omit", "-1", "x"):
            with self.subTest(device_bytes=size):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_DEVICE_BYTES=size)
                self.assertNothingShrunkOrEncrypted(result, "device size")


class TestEncryptGrowFailures(EncryptScript):
    def assertLeftClosedAndUnlabelled(self, result):
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("left unlabelled", result.stderr)
        calls = self.calls()
        self.assertIn("cryptsetup close " + SCRATCH, calls)
        self.assertFalse([c for c in calls if c.startswith("cryptsetup config")])

    def test_a_close_that_fails_is_reported_and_leaves_the_disk_unlabelled(self):
        # Under errexit a failing close would end the script with cryptsetup's
        # own code and no word of what went wrong.
        for inner in ({}, {"FAKE_RESIZE2FS_MAPPER_RC": "1"}):
            with self.subTest(inner_failure=inner):
                self.log.write_text("")
                result = self.run_encrypt(FAKE_CLOSE_RC="1", **inner)
                self.assertLeftClosedAndUnlabelled(result)
                self.assertIn("cryptsetup close %s failed" % SCRATCH, result.stderr)
                self.assertIn("still held", result.stderr)

    def test_a_failing_grow_closes_the_mapping_and_does_not_label(self):
        result = self.run_encrypt(FAKE_RESIZE2FS_MAPPER_RC="1")
        self.assertLeftClosedAndUnlabelled(result)

    def test_a_failing_zeroing_or_discard_closes_the_mapping_and_does_not_label(
        self,
    ):
        result = self.run_encrypt(FAKE_BLKDISCARD_RC="1")
        self.assertLeftClosedAndUnlabelled(result)
        self.assertEqual(len(self.discards()), 1)
        self.assertFalse(
            [c for c in self.calls() if c.startswith("resize2fs /dev/mapper")]
        )

    def test_a_failing_filesystem_check_closes_the_mapping_and_does_not_label(self):
        result = self.run_encrypt(FAKE_E2FSCK_MAPPER_RC="8")
        self.assertLeftClosedAndUnlabelled(result)


class UnlockScript(ShellCase):
    """yolobox-unlock against fake systemctl, journalctl and inotifywait.

    The fakes' streams are FIFOs the test writes into, so the test decides
    exactly which event reaches the helper next: inotify events, and the
    outcome of every blocking `systemctl start`. The test holds each FIFO open
    read-write: no open() ever blocks, and closing it gives the fake its EOF.
    """

    ASK_ID = "cryptsetup:/dev/vda2"
    ENCRYPT_ASK_ID = "yolobox:luks-encrypt"
    UNIT = "systemd-cryptsetup@root.service"
    ENCRYPT_UNIT = FACTS["luks_encrypt_unit"] + ".service"

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.ask_dir = self.tmp / "ask"
        self.ask_dir.mkdir()
        self.reply_log = self.tmp / "reply.log"
        self.journal_tail = self.tmp / "journal-tail"
        self.journal_tail.write_text("")
        self.fifos = {}
        for name in ("inotify", "ready", "start"):
            path = self.tmp / (name + ".fifo")
            os.mkfifo(path)
            fd = os.open(path, os.O_RDWR)
            self.addCleanup(self.close_fifo, name)
            self.fifos[name] = fd
        # A unit that is active or failed answers a start at once; any other
        # start announces itself on the ready FIFO and waits for the test to
        # send its exit code.
        self.fake(
            "systemctl",
            """#!/bin/sh
for unit; do :; done
state=$(cat "$FAKE_STATE/$unit" 2>/dev/null || echo "")
case "$1" in
  is-failed) [ "$state" = failed ] ;;
  is-active)
    case "$*" in *--quiet*) ;; *) echo "${state:-inactive}" ;; esac
    [ "$state" = active ]
    ;;
  reset-failed) echo "$*" >> "$FAKE_CHANGE_LOG"; rm -f "${FAKE_STATE:?}/${unit:?}" ;;
  start)
    shift
    echo "$*" >> "$FAKE_START_LOG"
    case "$*" in *--no-block*) exit 0 ;; esac
    case "$state" in active) exit 0 ;; failed) exit 1 ;; esac
    echo "start $*" > "$FAKE_READY_FIFO"
    read -r rc < "$FAKE_START_FIFO"
    exit "$rc"
    ;;
esac
""",
        )
        # A cursor for `-n 1 --show-cursor`; every other read is logged and
        # gets the journal the test wrote.
        self.fake(
            "journalctl",
            """#!/bin/sh
case "$*" in *--show-cursor*) echo "a line"; echo "-- cursor: s=fake"; exit 0 ;; esac
echo "$*" >> "$FAKE_JOURNAL_LOG"
cat "$FAKE_JOURNAL_TAIL"
""",
        )
        self.fake(
            "inotifywait",
            """#!/bin/sh
exec 3< "$FAKE_INOTIFY_FIFO"
if [ -n "${FAKE_INOTIFY_COULDNT_WATCH:-}" ]; then
  echo "Couldn't watch $FAKE_INOTIFY_COULDNT_WATCH: No such file or directory" >&2
  exit 1
fi
echo "Setting up watches." >&2
echo "Watches established." >&2
echo inotify-ready > "$FAKE_READY_FIFO"
while IFS= read -r path <&3; do
  echo "event"
done
""",
        )
        self.fake(
            "reply-password",
            """#!/bin/sh
if [ "$2" = "${FAKE_REPLY_FAILS:-}" ]; then
  [ -z "${FAKE_REPLY_WITHDRAWS:-}" ] || rm -f "$FAKE_REPLY_WITHDRAWS"
  exit 1
fi
body=$(cat; printf x)
body=${body%x}
printf 'reply %s %s [%s]\\n' "$1" "$2" "$body" >> "$FAKE_REPLY_LOG"
""",
        )
        self.env.update(
            {
                "YOLOBOX_" + key.upper(): str(code)
                for key, code in FACTS.items()
                if key.startswith("unlock_exit_")
            },
            YOLOBOX_UNLOCK_REPLY_PASSWORD=str(self.bin_dir / "reply-password"),
            YOLOBOX_UNLOCK_LUKS_DEVICE="/dev/fake",
            YOLOBOX_UNLOCK_UNIT=self.UNIT,
            YOLOBOX_UNLOCK_ENCRYPT_UNIT=self.ENCRYPT_UNIT,
            YOLOBOX_ASK_DIR=str(self.ask_dir),
            FAKE_STATE=str(self.state),
            FAKE_START_LOG=str(self.tmp / "start.log"),
            FAKE_CHANGE_LOG=str(self.tmp / "change.log"),
            FAKE_INOTIFY_FIFO=str(self.tmp / "inotify.fifo"),
            FAKE_READY_FIFO=str(self.tmp / "ready.fifo"),
            FAKE_START_FIFO=str(self.tmp / "start.fifo"),
            FAKE_JOURNAL_TAIL=str(self.journal_tail),
            FAKE_JOURNAL_LOG=str(self.tmp / "journal.log"),
            FAKE_REPLY_LOG=str(self.reply_log),
        )
        # The root mounts unless a test says otherwise.
        self.set_unit("sysroot.mount", "active")
        self.proc = None
        self.out = b""
        self.announced = b""
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
        try:
            self.proc.stdin.write(stdin.encode())
            self.proc.stdin.close()
        except BrokenPipeError:
            # A helper that refuses to start exits before reading its stdin;
            # what it said, and its exit code, are what the test then reads.
            pass
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

    def wait_for_announcement(self, wanted):
        """Block until a fake announces `wanted` on the ready FIFO. Earlier
        announcements stay for later waits; the one matched is consumed."""
        while True:
            lines = self.announced.split(b"\n")
            if wanted.encode() in lines[:-1]:
                lines.remove(wanted.encode())
                self.announced = b"\n".join(lines)
                return
            self.announced += self.read_fd(
                self.fifos["ready"], "%r (seen %r)" % (wanted, self.announced)
            )

    def wait_until_the_watch_is_up(self):
        """The inotifywait fake has opened its FIFO and written its first lines."""
        self.wait_for_announcement("inotify-ready")

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

    def wait_for_start(self, unit):
        """Block until the helper is inside a blocking `systemctl start unit`."""
        self.wait_for_announcement("start " + unit)

    def finish(self):
        out, err = self.proc.communicate(timeout=20)
        return self.proc.returncode, (self.out + out).decode(), err.decode()

    def write_ask(self, name, socket="/run/fake/reply.sock", ask_id=None):
        (self.ask_dir / name).write_text(
            "[Ask]\nPID=1\nSocket=%s\nId=%s\nMessage=Enter passphrase\n"
            % (socket, ask_id or self.ASK_ID)
        )

    def answered(self, ask_id=None):
        self.wait_for_stdout_line(
            "yolobox-unlock: answered id=%s" % (ask_id or self.ASK_ID)
        )

    def started_units(self):
        log = self.tmp / "start.log"
        return log.read_text().splitlines() if log.exists() else []

    def unit_changes(self):
        log = self.tmp / "change.log"
        return log.read_text().splitlines() if log.exists() else []

    def replies(self):
        if not self.reply_log.exists():
            return []
        return self.reply_log.read_text().splitlines()


class TestUnlockAnswersAsks(UnlockScript):
    def test_a_setting_the_box_did_not_pass_in_is_refused_by_name(self):
        for name in (
            "YOLOBOX_UNLOCK_EXIT_OPEN_FAILED",
            "YOLOBOX_ASK_DIR",
            "YOLOBOX_UNLOCK_LUKS_DEVICE",
            "YOLOBOX_UNLOCK_REPLY_PASSWORD",
        ):
            with self.subTest(name=name):
                value = self.env.pop(name)
                self.start()
                rc, out, err = self.finish()
                self.env[name] = value
                self.assertNotEqual(rc, 0)
                self.assertIn(name, err)
                self.assertEqual(self.started_units(), [])

    def test_an_ask_present_at_start_is_answered_with_exactly_the_passphrase(self):
        self.write_ask("ask.a")
        self.start()
        self.answered()
        self.assertEqual(
            self.replies(), ["reply 1 /run/fake/reply.sock [%s]" % PASSPHRASE]
        )
        self.wait_for_start(self.UNIT)
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("yolobox-unlock: unlocked\n", out)
        self.assertEqual(self.started_units(), [self.UNIT, "sysroot.mount"])
        self.assertNotIn(PASSPHRASE, out + err)

    def test_an_ask_posted_later_is_answered_when_inotify_reports_it(self):
        self.start()
        self.wait_until_the_watch_is_up()
        self.assertIsNone(self.proc.poll(), "exited with nothing to answer")
        self.assertEqual(self.replies(), [])
        self.write_ask("ask.b", socket="/run/fake/late.sock")
        self.send("inotify", str(self.ask_dir / "ask.b"))
        self.answered()
        self.assertEqual(
            self.replies(), ["reply 1 /run/fake/late.sock [%s]" % PASSPHRASE]
        )
        self.wait_for_start(self.UNIT)
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)

    def test_the_seeds_first_boot_answers_the_encryptions_ask_then_the_opens(self):
        # The encrypt unit, which the open requires, asks first and runs for a
        # long time; the open asks only once it is done. One start covers both.
        self.write_ask("ask.e", socket="/run/fake/e.sock", ask_id=self.ENCRYPT_ASK_ID)
        self.start()
        self.answered(self.ENCRYPT_ASK_ID)
        self.wait_for_start(self.UNIT)
        # The encryption runs. Its answered ask is still on disk when the
        # open's arrives (systemd removes it in its own time): answered once.
        self.assertEqual(len(self.replies()), 1)
        self.assertIsNone(self.proc.poll(), "gave up while the encryption ran")
        self.write_ask("ask.o", socket="/run/fake/o.sock")
        self.send("inotify", str(self.ask_dir / "ask.o"))
        self.answered()
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            [r.split()[2] for r in self.replies()],
            ["/run/fake/e.sock", "/run/fake/o.sock"],
        )
        self.assertEqual(self.started_units(), [self.UNIT, "sysroot.mount"])

    def test_an_ask_entry_that_cannot_be_read_is_skipped_and_the_other_answered(self):
        # systemd removes an ask the moment it is answered or withdrawn, so a
        # file can vanish between the glob and the read. A sed that fails the
        # way GNU sed does on an entry it cannot read; BSD sed shrugs at a
        # directory, so the failure is scripted instead of provoked.
        self.fake(
            "sed",
            "#!/bin/sh\nfor last; do :; done\n"
            'if [ "$last" = "$FAKE_UNREADABLE" ]; then\n'
            '  echo "sed: couldn\'t read $last" >&2\n'
            "  exit 4\n"
            "fi\n"
            'exec "$REAL_SED" "$@"\n',
        )
        unreadable = self.ask_dir / "ask.a"
        unreadable.write_text("")
        self.write_ask("ask.b", socket="/run/fake/b.sock")
        self.start(FAKE_UNREADABLE=str(unreadable), REAL_SED=shutil.which("sed"))
        self.answered()
        self.assertEqual(self.replies(), ["reply 1 /run/fake/b.sock [%s]" % PASSPHRASE])
        self.wait_for_start(self.UNIT)
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("ask.a could not be read, skipped", err)

    def test_a_reply_to_an_ask_withdrawn_meanwhile_is_skipped_and_the_next_answered(
        self,
    ):
        self.write_ask("ask.a", socket="/run/fake/gone.sock")
        self.write_ask("ask.b", socket="/run/fake/b.sock")
        self.start(
            FAKE_REPLY_FAILS="/run/fake/gone.sock",
            FAKE_REPLY_WITHDRAWS=str(self.ask_dir / "ask.a"),
        )
        self.answered()
        self.assertEqual(self.replies(), ["reply 1 /run/fake/b.sock [%s]" % PASSPHRASE])
        self.wait_for_start(self.UNIT)
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("withdrawn before the reply, skipped", err)


class TestUnlockOutcomes(UnlockScript):
    def test_an_already_open_disk_is_reported_unlocked_without_an_answer(self):
        self.set_unit(self.UNIT, "active")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("yolobox-unlock: unlocked", out)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.started_units(), ["sysroot.mount"])

    def test_a_cryptsetup_unit_that_fails_after_the_answer_is_the_wrong_key_naming_the_passphrase(
        self,
    ):
        self.journal_tail.write_text(
            "Failed to activate with specified passphrase. (Passphrase incorrect?)\n"
        )
        self.write_ask("ask.a")
        self.start()
        self.answered()
        self.wait_for_start(self.UNIT)
        self.send("start", "1")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("wrong_key"), err)
        self.assertIn("the passphrase does not open /dev/fake", err)
        self.assertNotIn("unlocked", out)
        self.assertNotIn("sysroot.mount", self.started_units())

    def test_a_start_that_fails_because_the_encryption_did_is_an_encrypt_failure_with_its_journal(
        self,
    ):
        self.write_ask("ask.e", ask_id=self.ENCRYPT_ASK_ID)
        self.journal_tail.write_text("yolobox: /dev/vda2 is too full\n")
        self.start()
        self.answered(self.ENCRYPT_ASK_ID)
        self.wait_for_start(self.UNIT)
        self.set_unit(self.ENCRYPT_UNIT, "failed")
        self.send("start", "1")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("encrypt_failed"), err)
        self.assertIn("yolobox: /dev/vda2 is too full", err)
        self.assertIn("the in-place encryption of /dev/fake failed", err)
        self.assertNotIn("does not open", err)
        self.assertNotIn("unlocked", out)

    def test_an_encryption_that_already_failed_is_an_encrypt_failure_before_anything_is_answered(
        self,
    ):
        # Nothing may run it again: the box is recreated.
        self.set_unit(self.ENCRYPT_UNIT, "failed")
        self.set_unit(self.UNIT, "failed")
        self.write_ask("ask.a")
        self.journal_tail.write_text("yolobox-luks-encrypt: failed (exit 3)\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("encrypt_failed"), err)
        self.assertIn("yolobox-luks-encrypt: failed (exit 3)", err)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.unit_changes(), [])
        self.assertEqual(self.started_units(), [])

    def test_a_start_that_fails_without_ever_asking_is_judged_at_once(self):
        # Nothing to answer: the helper must not wait for an ask that never
        # comes, but read the start's verdict the moment it ends.
        self.start()
        self.wait_for_start(self.UNIT)
        self.wait_until_the_watch_is_up()
        self.assertIsNone(self.proc.poll())
        for encrypt_state, code, said in (
            ("", "open_failed", "unlock failed (exit 1)"),
            ("failed", "encrypt_failed", "the in-place encryption of /dev/fake failed"),
        ):
            with self.subTest(encrypt_state=encrypt_state):
                if self.proc.poll() is not None:
                    self.start()
                    self.wait_for_start(self.UNIT)
                    self.wait_until_the_watch_is_up()
                if encrypt_state:
                    self.set_unit(self.ENCRYPT_UNIT, encrypt_state)
                self.send("start", "1")
                rc, out, err = self.finish()
                self.assertEqual(rc, exit_code(code), err)
                self.assertIn(said, err)
                self.assertEqual(self.replies(), [])
                self.assertNotIn("unlocked", out)
                self.assertNotIn("sysroot.mount", self.started_units())

    def test_an_open_that_fails_not_for_the_key_is_an_open_failure_with_this_attempts_journal(
        self,
    ):
        # A device timeout or a cancelled job is no wrong key: yo must not say
        # the key in 1Password does not open the disk.
        self.journal_tail.write_text("Timed out waiting for device /dev/vda2.\n")
        self.write_ask("ask.a")
        self.start()
        self.answered()
        self.wait_for_start(self.UNIT)
        self.send("start", "5")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("open_failed"), err)
        self.assertIn("Timed out waiting for device /dev/vda2.", err)
        self.assertIn("unlock failed (exit 5)", err)
        self.assertNotIn("does not open", err)
        journal_reads = (self.tmp / "journal.log").read_text()
        self.assertIn("--after-cursor=s=fake", journal_reads)

    def test_an_answered_ask_posted_again_is_the_wrong_key_naming_the_passphrase(self):
        self.write_ask("ask.a")
        self.start()
        self.answered()
        self.wait_for_start(self.UNIT)
        self.write_ask("ask.again")
        self.send("inotify", str(self.ask_dir / "ask.again"))
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("wrong_key"), err)
        self.assertIn("asked again after it was answered", err)
        self.assertEqual(len(self.replies()), 1)

    def test_a_start_that_ends_well_without_asking_is_unlocked(self):
        self.start()
        self.wait_for_start(self.UNIT)
        self.wait_until_the_watch_is_up()
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("yolobox-unlock: unlocked", out)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.started_units(), [self.UNIT, "sysroot.mount"])

    def test_a_root_mount_that_fails_is_a_mount_failure_with_its_journal(self):
        self.set_unit(self.UNIT, "active")
        self.set_unit("sysroot.mount", "failed")
        self.journal_tail.write_text("mount: /sysroot: Can't open blockdev\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("mount_failed"), err)
        self.assertIn("/dev/fake opened but sysroot.mount failed", err)
        self.assertIn("mount: /sysroot: Can't open blockdev", err)
        self.assertNotIn("unlocked", out)

    def test_a_root_mounted_only_on_retry_after_emergency_mode_is_a_mount_failure(self):
        self.set_unit(self.UNIT, "active")
        self.set_unit("emergency.target", "active")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("mount_failed"), err)
        self.assertIn("emergency mode", err)
        self.assertNotIn("unlocked", out)

    def test_a_wrong_key_on_an_earlier_attempt_is_reset_and_asked_again(self):
        self.set_unit(self.UNIT, "failed")
        self.start()
        self.wait_until_the_watch_is_up()
        self.wait_for_start(self.UNIT)
        self.assertEqual(
            (self.unit_changes(), self.started_units()),
            (["reset-failed " + self.UNIT], [self.UNIT]),
        )
        self.write_ask("ask.new")
        self.send("inotify", str(self.ask_dir / "ask.new"))
        self.answered()
        self.send("start", "0")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)

    def test_an_empty_passphrase_is_refused_before_anything_starts(self):
        for stdin in ("", "\n"):
            with self.subTest(stdin=stdin):
                self.start(stdin=stdin)
                rc, out, err = self.finish()
                self.assertEqual(rc, exit_code("no_passphrase"), err)
                self.assertIn("no passphrase", err)
                self.assertEqual(self.replies(), [])
                self.assertEqual(self.started_units(), [])

    def test_a_watch_inotifywait_could_not_set_up_is_fatal(self):
        self.start(FAKE_INOTIFY_COULDNT_WATCH="/run/fake/ask-password")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("watch_failed"), err)
        self.assertIn("Couldn't watch /run/fake/ask-password", err)

    def test_the_watch_ending_with_nothing_answered_is_a_watch_failure_and_ends_the_start(
        self,
    ):
        self.start()
        self.wait_until_the_watch_is_up()
        self.wait_for_start(self.UNIT)
        self.close_fifo("inotify")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("watch_failed"), err)
        self.assertIn("watch ended", err)
        self.assertNotIn("unlocked", out)
        # finish() returning proves the blocking start died with the helper:
        # it holds the helper's stdout and stderr, as it would the ssh session.

    def test_a_missing_ask_directory_is_created_before_it_is_watched(self):
        self.ask_dir.rmdir()
        self.start()
        self.wait_until_the_watch_is_up()
        self.assertTrue(self.ask_dir.is_dir())

    def test_a_reply_that_fails_is_unexpected_and_mounts_nothing(self):
        self.write_ask("ask.a")
        self.start(FAKE_REPLY_FAILS="/run/fake/reply.sock")
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("unexpected"), err)
        self.assertIn("reply to %s failed" % self.ASK_ID, err)
        self.assertNotIn("answered", out)
        self.assertNotIn("sysroot.mount", self.started_units())

    def test_a_failure_the_script_has_no_verdict_for_is_unexpected_naming_the_command(
        self,
    ):
        self.set_unit(self.UNIT, "active")
        self.set_unit("sysroot.mount", "failed")
        self.fake("journalctl", "#!/bin/sh\nexit 9\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, exit_code("unexpected"), err)
        self.assertIn("unexpected failure (exit 9) at line", err)
        self.assertNotIn("unlocked", out)


class InitrdKeysScript(ShellCase):
    def setUp(self):
        super().setUp()
        # The mountpoint is the last argument; the "ISO" is one user-data file.
        # Like libmount, the fake creates its run directory under the umask.
        self.fake(
            "mount",
            '#!/bin/sh\nmkdir -p "$FAKE_RUN_MOUNT"\n'
            'for last; do :; done\ncp "$FAKE_USER_DATA" "$last/user-data"\n',
        )
        self.fake("umount", '#!/bin/sh\nrm -f "$1/user-data"\n')
        self.user_data = self.tmp / "user-data"
        self.out = self.tmp / "keys"
        self.run_mount = self.tmp / "run-mount"
        self.env.update(
            FAKE_RUN_MOUNT=str(self.run_mount), FAKE_USER_DATA=str(self.user_data)
        )

    def run_keys(self, *args):
        # The initrd's own umask: what mount must see.
        return self.run_bash(KEYS, *args, preexec_fn=lambda: os.umask(0o022))

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
            "  - name: user\n"
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
    def test_the_mount_leaves_its_run_directory_readable_to_users(self):
        # /run/mount outlives switch-root; 0700 denies user managers their watch.
        self.user_data.write_text(
            "users:\n  - name: user\n    ssh-authorized-keys:\n      - ssh-ed25519 AAAA1\n"
        )
        result = self.run_keys(str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(oct(self.run_mount.stat().st_mode & 0o777), "0o755")

    @unittest.skipUnless(shutil.which("gawk"), "lima's key program needs gawk")
    def test_user_data_without_a_key_fails_instead_of_authorizing_nobody(self):
        self.user_data.write_text("#cloud-config\nusers:\n  - name: user\n")
        result = self.run_keys(str(self.out))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("no ssh-authorized-keys", result.stderr)


if __name__ == "__main__":
    unittest.main()
