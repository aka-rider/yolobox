"""Behaviour of the three initrd scripts in nix/luks/, run for real under bash.

Nix builds each with writeShellApplication, which prepends
`set -o errexit -o nounset -o pipefail` and puts `runtimeInputs` on PATH. Here
bash gets the same three options and PATH holds a scratch directory of fake
tools ahead of the system ones. The fakes append their argv to a log file, so
the tests read what the scripts did to the disk and to systemd, not how they
did it.

Tools whose absence skips tests: gawk (the two tests of lima's key program).
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
PASSPHRASE = "k" * 64
# luksDump output per state. The label says finished, the subsystem says ours.
DUMP_FINISHED = "LUKS header information\nLabel:          %s\n" % LABEL
DUMP_UNFINALIZED = "LUKS header information\nSubsystem:      %s\n" % LABEL
DUMP_UNFINISHED = DUMP_UNFINALIZED + "Requirements:   online-reencrypt-v2\n"
DUMP_FOREIGN = "LUKS header information\nLabel:          other\n"


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
  status) echo "${FAKE_STATUS_OFFSET-  offset:  32768 sectors}" ;;
  reencrypt) echo "stdin_bytes $(wc -c | tr -d ' ')" >> "$FAKE_LOG"; exit "${FAKE_REENCRYPT_RC:-0}" ;;
  open)
    echo "stdin_bytes $(wc -c | tr -d ' ')" >> "$FAKE_LOG"
    # --test-passphrase rejects the first FAKE_REJECTS answers.
    case "$*" in *--test-passphrase*)
      n=$(cat "$FAKE_LOG.rejected" 2>/dev/null || echo 0)
      if [ "$n" -lt "${FAKE_REJECTS:-0}" ]; then echo $((n + 1)) > "$FAKE_LOG.rejected"; exit 2; fi
    esac
    ;;
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
exit "${FAKE_E2FSCK_RC:-1}"
""",
    "resize2fs": """
echo "resize2fs $*" >> "$FAKE_LOG"
if [ "$1" = -P ]; then
  echo "resize2fs 1.47 (fake)" >&2
  echo "Estimated minimum size of the filesystem: ${FAKE_FS_MIN-1000000}"
  exit 0
fi
exit "${FAKE_RESIZE2FS_RC:-0}"
""",
    "dumpe2fs": """
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
        # /sys/class/block/<dev>/../queue, with the offload on unless a test says not.
        self.sysfs = self.tmp / "sysfs"
        (self.sysfs / "fake").mkdir(parents=True)
        (self.sysfs / "queue").mkdir()
        self.set_write_zeroes_max("33554432")
        self.env = {
            "PATH": os.pathsep.join(
                [str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
            "FAKE_LOG": str(self.log),
            "FAKE_LUKSDUMP": str(self.luksdump),
            "FAKE_DEVICE_BYTES": str(DEVICE_BYTES),
            "FAKE_ISLUKS_RC": "1",
            "FAKE_BLKID_TYPE": "ext4",
            "YOLOBOX_SYSFS_BLOCK": str(self.sysfs),
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

    def set_write_zeroes_max(self, text):
        (self.sysfs / "queue" / "write_zeroes_max_bytes").write_text(text)

    def asks(self):
        return [c for c in self.calls() if c.startswith("systemd-ask-password")]

    def asked_for_a_passphrase(self):
        return bool(self.asks())

    def discards(self):
        return [c for c in self.calls() if c.startswith("blkdiscard")]


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
            "blkdiscard -z -f --offset %d" % TAIL,
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
        # The header is marked as this script's from its first byte; the label
        # comes only with finalize.
        self.assertIn("--subsystem %s " % LABEL, reencrypt)
        # vda2's length is not a multiple of 4096: that sector size would fail
        # after the data was already rewritten.
        self.assertNotIn("--sector-size", reencrypt)

    def test_the_tail_past_the_filesystem_is_zeroed_then_discarded_to_the_end(self):
        # Discard is advisory (qemu's default ignores it), so the plaintext is
        # overwritten first; the discard after it gives the image its holes.
        result = self.run_migrate()
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
                result = self.run_migrate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.discards()), 1)
                self.assertNotIn("-z", self.discards()[0])
                self.assertIn("offloads no write-zeroes", result.stdout)
        (self.sysfs / "queue" / "write_zeroes_max_bytes").unlink()
        self.log.write_text("")
        result = self.run_migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.discards()), 1)
        self.assertIn("offloads no write-zeroes (unknown)", result.stdout)

    def test_the_passphrase_reaches_cryptsetup_without_a_trailing_newline(self):
        self.run_migrate()
        self.assertEqual(
            [c for c in self.calls() if c.startswith("stdin_bytes")],
            ["stdin_bytes 64", "stdin_bytes 64"],
        )

    def test_the_passphrase_is_asked_for_by_name_and_never_printed(self):
        result = self.run_migrate()
        (ask,) = self.asks()
        self.assertIn("--id=yolobox:luks-migrate", ask)
        self.assertNotIn(
            PASSPHRASE, result.stdout + result.stderr + "\n".join(self.calls())
        )

    def test_a_device_too_full_to_shrink_stops_before_touching_it(self):
        result = self.run_migrate(FAKE_DEVICE_BYTES=str(REGION + 32 * MIB - 1))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("too full", result.stderr)
        self.assertIn("%s is still plaintext" % DEVICE, result.stderr)
        self.assertIn("systemd-cryptsetup@%s.service" % MAPPER, result.stderr)
        # The port differs per instance; the script must not guess one.
        self.assertNotRegex(result.stderr, r"\bport\b")
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

    def test_a_plain_disk_cannot_verify_the_passphrase_so_the_first_answer_is_used(
        self,
    ):
        self.run_migrate(FAKE_REJECTS="5")
        self.assertEqual(len(self.asks()), 1)
        self.assertFalse([c for c in self.calls() if "--test-passphrase" in c])


class TestMigrateReportsTheDiskState(MigrateScript):
    """The last journal line of a failed run says what the disk holds now;
    yolobox-unlock relays it, and "plaintext" is claimed only when true."""

    def test_a_failure_before_the_first_ciphertext_says_still_plaintext(self):
        result = self.run_migrate(FAKE_E2FSCK_RC="4")
        self.assertIn(
            "failed (exit 4); %s is still plaintext, not encrypted" % DEVICE,
            result.stderr,
        )

    def test_a_failure_while_encrypting_says_partly_encrypted(self):
        result = self.run_migrate(FAKE_REENCRYPT_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("%s is partly encrypted" % DEVICE, result.stderr)
        self.assertIn("the next boot resumes", result.stderr)

    def test_a_failure_while_resuming_says_partly_encrypted(self):
        self.luks(DUMP_UNFINISHED)
        result = self.run_migrate(FAKE_REENCRYPT_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("%s is partly encrypted" % DEVICE, result.stderr)

    def test_a_failure_in_finalize_says_encrypted_but_not_finalized(self):
        for dump in (None, DUMP_UNFINISHED, DUMP_UNFINALIZED):
            with self.subTest(dump=dump):
                if dump is not None:
                    self.luks(dump)
                result = self.run_migrate(FAKE_BLKDISCARD_RC="1")
                self.assertEqual(result.returncode, 5)
                self.assertIn(
                    "%s is encrypted but not finalized; the next boot finishes it"
                    % DEVICE,
                    result.stderr,
                )
                self.assertNotIn("plaintext", result.stderr)

    def test_a_run_that_succeeds_or_has_nothing_to_do_reports_no_failure(self):
        self.assertNotIn("failed", self.run_migrate().stderr)
        self.luks(DUMP_FINISHED)
        self.assertEqual(self.run_migrate().stderr, "")


class TestMigrateStates(MigrateScript):
    def test_an_unfinished_reencryption_is_resumed_then_finalized(self):
        self.luks(DUMP_UNFINISHED)
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
        # With or without the subsystem mark: disks finished before the mark
        # existed carry only the label.
        for dump in (DUMP_FINISHED, DUMP_FINISHED + "Subsystem:      %s\n" % LABEL):
            with self.subTest(dump=dump):
                self.luks(dump)
                result = self.run_migrate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.asked_for_a_passphrase())
                self.assertFalse(
                    [c for c in self.calls() if c.startswith("cryptsetup open")]
                )

    def test_a_luks_disk_that_is_not_marked_as_ours_is_refused_untouched(self):
        # Finalizing it would discard everything past a filesystem it may not hold.
        for dump in (
            DUMP_FOREIGN,
            "LUKS header information\n",
            "LUKS header information\nSubsystem:      other\n",
            "LUKS header information\nSubsystem:      other\nRequirements:   online-reencrypt-v2\n",
        ):
            with self.subTest(dump=dump):
                self.log.write_text("")
                self.luks(dump)
                result = self.run_migrate()
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("not marked as a yolobox migration", result.stderr)
                self.assertFalse(self.asked_for_a_passphrase())
                calls = self.calls()
                self.assertFalse(
                    [
                        c
                        for c in calls
                        if c.startswith(("cryptsetup open", "blkdiscard"))
                    ]
                )
                self.assertFalse(
                    [c for c in calls if c.startswith("cryptsetup config")]
                )

    def test_a_marked_luks_disk_without_the_label_is_finalized_only(self):
        self.luks(DUMP_UNFINALIZED)
        result = self.run_migrate()
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

    def test_an_empty_answer_is_refused(self):
        result = self.run_migrate(FAKE_ASK_EMPTY="1")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no passphrase given", result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("cryptsetup open")])

    def test_a_passphrase_that_does_not_open_an_unfinished_disk_is_asked_again(self):
        # A rejected answer must not fail the unit: the unit would stay failed
        # and the next `yo up`, with the right key, could only read the journal.
        for dump in (DUMP_UNFINISHED, DUMP_UNFINALIZED):
            with self.subTest(dump=dump):
                self.log.write_text("")
                (self.log.parent / "log.rejected").unlink(missing_ok=True)
                self.luks(dump)
                result = self.run_migrate(FAKE_REJECTS="2")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.asks()), 3)
                self.assertEqual(
                    result.stderr.count("does not open %s, asking again" % DEVICE), 2
                )
                self.assertNotIn("failed", result.stderr)
                calls = self.calls()
                self.assertEqual(len([c for c in calls if "--test-passphrase" in c]), 3)
                # Nothing is written before a passphrase that opens the disk.
                before_accept = calls[: calls.index(self.asks()[-1])]
                self.assertFalse(
                    [
                        c
                        for c in before_accept
                        if c.startswith(("cryptsetup reencrypt", "blkdiscard"))
                    ]
                )

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
        # A --test-passphrase open names no mapping.
        mapping_calls = [
            c
            for c in self.calls()
            if (
                c.startswith(
                    ("cryptsetup open", "cryptsetup close", "cryptsetup status")
                )
                and "--test-passphrase" not in c
            )
            or "/dev/mapper/" in c
        ]
        self.assertTrue(mapping_calls)
        for call in mapping_calls:
            self.assertRegex(call, r"\b%s\b" % SCRATCH)
            self.assertNotRegex(call, r"(^|[ /])%s\b(?!-)" % MAPPER)

    def test_every_state_that_finalizes_uses_a_private_mapper_name(self):
        for name, dump in (
            ("encrypt", None),
            ("resume", DUMP_UNFINISHED),
            ("finalize", DUMP_UNFINALIZED),
        ):
            with self.subTest(state=name):
                self.log.write_text("")
                if dump is not None:
                    self.luks(dump)
                self.assertOnlyTheScratchNameIsUsed(self.run_migrate())

    def test_a_failed_finalize_has_not_opened_the_real_mapping_either(self):
        self.luks(DUMP_UNFINALIZED)
        result = self.run_migrate(FAKE_RESIZE2FS_RC="1")
        self.assertEqual(result.returncode, 5, result.stderr)
        opened = [
            c
            for c in self.calls()
            if c.startswith("cryptsetup open") and "--test-passphrase" not in c
        ]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].endswith(" " + SCRATCH), opened)


class TestMigrateRefusesToDiscardOnUnreadableNumbers(MigrateScript):
    """bash reads "" as 0, which would discard the whole device from byte 0."""

    def setUp(self):
        super().setUp()
        self.luks(DUMP_UNFINALIZED)

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
                result = self.run_migrate(FAKE_STATUS_OFFSET=output)
                self.assertNothingDiscarded(result, "offset_sectors")

    def test_a_filesystem_dumpe2fs_reports_as_empty_stops_before_the_discard(self):
        for count in ("0", "-5", "x"):
            with self.subTest(block_count=count):
                self.log.write_text("")
                result = self.run_migrate(FAKE_BLOCK_COUNT=count)
                self.assertNothingDiscarded(result, "filesystem block count")


class TestMigrateRefusesToEncryptOnUnreadableNumbers(MigrateScript):
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

    def test_a_block_size_dumpe2fs_does_not_report_stops_before_resize2fs(self):
        for size in ("omit", "0", "x"):
            with self.subTest(block_size=size):
                self.log.write_text("")
                result = self.run_migrate(FAKE_BLOCK_SIZE=size)
                self.assertNothingShrunkOrEncrypted(result, "filesystem block size")

    def test_a_minimum_size_resize2fs_does_not_report_stops_before_resize2fs(self):
        for size in ("", "lizard", "0"):
            with self.subTest(minimum=size):
                self.log.write_text("")
                result = self.run_migrate(FAKE_FS_MIN=size)
                self.assertNothingShrunkOrEncrypted(result, "filesystem minimum size")

    def test_a_device_size_blockdev_does_not_report_stops_before_resize2fs(self):
        for size in ("omit", "-1", "x"):
            with self.subTest(device_bytes=size):
                self.log.write_text("")
                result = self.run_migrate(FAKE_DEVICE_BYTES=size)
                self.assertNothingShrunkOrEncrypted(result, "device size")


class TestMigrateFinalizeFailures(MigrateScript):
    def setUp(self):
        super().setUp()
        self.luks(DUMP_UNFINALIZED)

    def assertLeftClosedAndUnlabelled(self, result):
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("left unlabelled", result.stderr)
        calls = self.calls()
        self.assertIn("cryptsetup close " + SCRATCH, calls)
        self.assertFalse([c for c in calls if c.startswith("cryptsetup config")])

    def test_a_close_that_fails_is_reported_and_leaves_the_disk_unlabelled(self):
        # Under errexit a failing close would end the script with cryptsetup's
        # own code and no word of what went wrong.
        for inner in ({}, {"FAKE_RESIZE2FS_RC": "1"}):
            with self.subTest(inner_failure=inner):
                self.log.write_text("")
                result = self.run_migrate(FAKE_CLOSE_RC="1", **inner)
                self.assertLeftClosedAndUnlabelled(result)
                self.assertIn("cryptsetup close %s failed" % SCRATCH, result.stderr)
                self.assertIn("still held", result.stderr)

    def test_a_failing_grow_closes_the_mapping_and_does_not_label(self):
        self.assertLeftClosedAndUnlabelled(self.run_migrate(FAKE_RESIZE2FS_RC="1"))

    def test_a_failing_zeroing_or_discard_closes_the_mapping_and_does_not_label(
        self,
    ):
        result = self.run_migrate(FAKE_BLKDISCARD_RC="1")
        self.assertLeftClosedAndUnlabelled(result)
        self.assertEqual(len(self.discards()), 1)
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
        bin_dir = self.bin_dir = self.tmp / "bin"
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
  reset-failed) echo "$*" >> "$FAKE_CHANGE_LOG"; rm -f "$FAKE_STATE/$unit" ;;
  restart) echo "$*" >> "$FAKE_CHANGE_LOG" ;;
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
    if [ -n "${FAKE_FOLLOW_FAILS:-}" ]; then
      echo "journalctl: cannot read the journal" >&2
      exit 9
    fi
    exec 3< "$FAKE_JOURNAL_FIFO"
    echo journal-ready > "$FAKE_READY_FIFO"
    exec cat <&3
  fi
done
if [ -n "${FAKE_JOURNAL_FAILS:-}" ]; then
  echo "journalctl: cannot read the journal" >&2
  exit 9
fi
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
            "FAKE_CHANGE_LOG": str(self.tmp / "change.log"),
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

    def unit_changes(self):
        """What the helper did to unit state: reset-failed and restart calls."""
        log = self.tmp / "change.log"
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

    def test_each_ask_with_its_own_id_is_answered_separately(self):
        self.write_ask("ask.a", socket="/run/fake/a.sock")
        self.write_ask(
            "ask.b", socket="/run/fake/b.sock", ask_id="yolobox:luks-migrate"
        )
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertEqual(
            sorted(r.split()[2] for r in self.replies()),
            ["/run/fake/a.sock", "/run/fake/b.sock"],
        )

    def test_an_id_asked_again_after_its_answer_is_a_rejected_passphrase(self):
        # systemd-cryptsetup re-asks a wrong passphrase up to its tries and the
        # migration re-asks until one opens the disk: the second ask is the
        # verdict, and no second argon2 run is bought with the same answer.
        self.write_ask("ask.a")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertEqual(len(self.replies()), 1)
        (self.ask_dir / "ask.a").unlink()
        self.write_ask("ask.b", socket="/run/fake/again.sock")
        self.send("inotify", str(self.ask_dir / "ask.b"))
        rc, out, err = self.finish()
        self.assertEqual(rc, 1, err)
        self.assertIn("the passphrase does not open /dev/fake", err)
        self.assertIn("%s asked again" % self.ASK_ID, err)
        self.assertEqual(len(self.replies()), 1)
        self.assertNotIn("unlocked", out)

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
        self.assertIn("the in-place encryption of /dev/fake failed", err)
        self.assertIn("ends with the state it left the disk in", err)
        self.assertNotIn("unlocked", out)

    def test_a_cryptsetup_unit_that_fails_after_the_answer_exits_1_naming_the_passphrase(
        self,
    ):
        self.write_ask("ask.a")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.set_unit("systemd-cryptsetup@root.service", "failed")
        self.send("journal", "Failed")
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

    def test_a_root_mounted_only_on_retry_after_emergency_mode_exits_5(self):
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.set_unit("emergency.target", "active")
        self.journal_tail.write_text("Failed to mount /sysroot\n")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 5, err)
        self.assertIn("sysroot.mount mounted only on retry", err)
        self.assertIn("emergency mode", err)
        self.assertIn("Failed to mount /sysroot", err)
        self.assertIn("journalctl -b", err)
        self.assertIn("stop and start the instance", err)
        self.assertNotIn("unlocked", out)

    def test_the_root_mount_is_not_awaited_while_the_disk_is_still_closed(self):
        self.start()
        self.wait_until_streams_are_up()
        self.set_unit("systemd-cryptsetup@root.service", "failed")
        self.send("journal", "Failed")
        self.finish()
        self.assertEqual(self.started_units(), [])

    def test_a_failure_inside_a_function_exits_6_naming_the_command(self):
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.set_unit("sysroot.mount", "failed")
        self.start(FAKE_JOURNAL_FAILS="1")
        rc, out, err = self.finish()
        self.assertEqual(rc, 6, err)
        self.assertIn("unexpected failure (exit 9)", err)
        self.assertIn("journalctl", err)
        self.assertNotIn("unlocked", out)

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

    def assertOneWriterEndingExits3AtOnce(self, fifo, writer):
        # The FIFO reaches EOF only when both writers are gone, so a lone dead
        # writer used to leave the helper waiting until yo's deadline.
        self.start()
        self.wait_until_streams_are_up()
        self.close_fifo(fifo)
        rc, out, err = self.finish()
        self.assertEqual(rc, 3, err)
        self.assertIn("%s ended (exit 0)" % writer, err)
        self.assertNotIn("unlocked", out)

    def test_the_journal_feed_ending_with_nothing_unlocked_exits_3_at_once(self):
        self.assertOneWriterEndingExits3AtOnce("journal", "journalctl")

    def test_the_ask_watch_ending_with_nothing_unlocked_exits_3_at_once(self):
        self.assertOneWriterEndingExits3AtOnce("inotify", "inotifywait")

    def test_a_journal_feed_that_fails_to_start_is_reported_with_its_own_words(self):
        self.start(FAKE_FOLLOW_FAILS="1")
        rc, out, err = self.finish()
        self.assertEqual(rc, 3, err)
        self.assertIn("journalctl ended (exit 9)", err)
        self.assertIn("journalctl: cannot read the journal", out + err)


class TestUnlockAfterAFailedAttempt(UnlockScript):
    """`yo up` again after a wrong key: the cryptsetup unit is still failed."""

    UNIT = "systemd-cryptsetup@root.service"

    def test_a_unit_failed_at_startup_is_reset_and_started_again_once(self):
        self.set_unit(self.UNIT, "failed")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertIsNone(self.proc.poll(), "judged the old failure as this attempt's")
        self.assertEqual(
            self.unit_changes(),
            ["reset-failed " + self.UNIT, "restart --no-block " + self.UNIT],
        )
        self.write_ask("ask.new")
        self.send("inotify", str(self.ask_dir / "ask.new"))
        self.wait_for_stdout_line("yolobox-unlock: answered id=%s" % self.ASK_ID)
        self.set_unit(self.UNIT, "active")
        self.send("journal", "Finished")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.unit_changes()), 2)

    def test_a_second_failure_after_the_restart_still_exits_1(self):
        self.set_unit(self.UNIT, "failed")
        self.start()
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.set_unit(self.UNIT, "failed")
        self.send("journal", "Failed")
        rc, out, err = self.finish()
        self.assertEqual(rc, 1, err)
        self.assertEqual(len(self.unit_changes()), 2)

    def test_a_unit_that_has_not_failed_is_left_alone(self):
        self.set_unit(self.UNIT, "active")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.unit_changes(), [])

    def test_a_failed_migration_is_reported_and_not_restarted(self):
        self.set_unit("yolobox-luks-migrate.service", "failed")
        self.set_unit(self.UNIT, "failed")
        self.start()
        rc, out, err = self.finish()
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.unit_changes(), [])


class TestUnlockAskFileRace(UnlockScript):
    def test_an_ask_entry_that_cannot_be_read_is_skipped_and_the_others_answered(self):
        # A sed that fails the way GNU sed does on an entry it cannot read (exit
        # 4, a code the helper documents for another outcome; a vanished file
        # fails the same way). BSD sed shrugs at a directory, so the failure is
        # scripted instead of provoked with a real file.
        write_fake(
            self.bin_dir,
            "sed",
            "for last; do :; done\n"
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
        self.wait_until_streams_are_up()
        self.sync_with_the_loop()
        self.assertIsNone(self.proc.poll(), "exited on an unreadable ask entry")
        self.assertEqual(self.replies(), ["reply 1 /run/fake/b.sock [%s]" % PASSPHRASE])
        self.set_unit("systemd-cryptsetup@root.service", "active")
        self.send("journal", "Finished")
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, err)
        self.assertIn("ask.a could not be read, skipped", err)


class InitrdKeysScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        # The mountpoint is the last argument; the "ISO" is one user-data file.
        # Like libmount, the fake creates its run directory under the umask.
        write_fake(
            bin_dir,
            "mount",
            'mkdir -p "$FAKE_RUN_MOUNT"\n'
            'for last; do :; done\ncp "$FAKE_USER_DATA" "$last/user-data"\n',
        )
        write_fake(bin_dir, "umount", 'rm -f "$1/user-data"\n')
        self.user_data = self.tmp / "user-data"
        self.out = self.tmp / "keys"
        self.run_mount = self.tmp / "run-mount"
        self.env = {
            "FAKE_RUN_MOUNT": str(self.run_mount),
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
            # The initrd's own umask: what mount must see.
            preexec_fn=lambda: os.umask(0o022),
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
