# shellcheck shell=bash
# Runs in the initrd as yolobox-luks-migrate.service, before the root device is
# opened: encrypts the plain ext4 root in place, resumes an interrupted
# encryption, or finishes one, depending on what it finds. A disk that already
# carries the label is left alone and nothing is asked. See nix/luks.nix.
if [ "$#" -ne 3 ]; then
  echo "usage: yolobox-luks-migrate <device> <label> <mapper>" >&2
  exit 2
fi
device=$1
label=$2
mapper=$3

mib=$((1024 * 1024))
# The filesystem is shrunk to its minimum plus this much, in percent.
headroom_percent=5
# Room left for the LUKS2 header: the reencryption shortens the device by this
# much, and the encrypted region is padded by it.
header_room_mib=32
header_room=$((header_room_mib * mib))
align=$((4 * mib))
# cryptsetup reports offsets in sectors of this size whatever the encryption
# sector size is.
sector_bytes=512
progress_seconds=30
# Where the kernel says whether the disk offloads write-zeroes; a test points
# it elsewhere.
sysfs_block="${YOLOBOX_SYSFS_BLOCK:-/sys/class/block}"

# What the disk holds right now, for the failure line below. Every write that
# changes it updates this first, so the line is right whatever fails after.
disk_state="still plaintext, not encrypted"
report_failure() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "yolobox-luks-migrate: failed (exit ${rc}); ${device} is ${disk_state}" >&2
  fi
}
trap report_failure EXIT

# Asks until a passphrase arrives; with $1 = verify, until one that opens the
# LUKS device. A rejected passphrase fails nothing: the unit stays at its ask
# and the next `yo up` answers it (yolobox-unlock exits 1 on the second ask).
# A plain ext4 device has nothing to verify against, so encrypt trusts the
# first answer.
read_key() {
  while :; do
    key=$(systemd-ask-password --no-tty --timeout=0 --id=yolobox:luks-migrate "yolobox: passphrase to encrypt ${device} in place")
    if [ -z "$key" ]; then
      echo "yolobox: no passphrase given" >&2
      exit 2
    fi
    if [ "$1" = trust ] || printf '%s' "$key" | cryptsetup open --test-passphrase --key-file - "$device"; then
      return 0
    fi
    echo "yolobox: the passphrase does not open ${device}, asking again" >&2
  done
}

fs_block_size() {
  dumpe2fs -h "$1" 2>/dev/null | awk -F: '/^Block size/ {gsub(/ /, "", $2); print $2}'
}

fs_block_count() {
  dumpe2fs -h "$1" 2>/dev/null | awk -F: '/^Block count/ {gsub(/ /, "", $2); print $2}'
}

# A number read from a tool's output, checked before it is used in arithmetic:
# bash reads an empty or non-numeric string as 0. In finalize that would move
# the discard offset to the start of the device; in encrypt it would collapse
# the region the encryption is limited to. $3 says what was left untouched.
require_positive_int() {
  case "$2" in
    '' | *[!0-9]* | 0*)
      echo "yolobox: ${1} is '${2}', not a positive integer; ${3}" >&2
      return 1
      ;;
  esac
}

# e2fsck exits 1 when it fixed something and 2 when a reboot is advised; both
# leave a consistent filesystem. Anything else does not.
fsck_ok() {
  local rc=0
  e2fsck -f -y "$1" || rc=$?
  if [ "$rc" -gt 2 ]; then
    echo "yolobox: e2fsck on $1 failed with $rc" >&2
    return 4
  fi
}

# A header field as luksDump prints it: `Label:          yolobox-luks`.
header_field() {
  awk -v name="$1" -F: '$1 == name {sub(/^[[:space:]]+/, "", $2); print $2; exit}' <<<"$dump"
}

# Sets the global `state`. Called plainly, never inside $(...), so its exit
# ends the script. The label says the disk is finished; the subsystem, written
# with the header at the start of encrypt, says it is ours and unfinished. A
# LUKS disk with neither was not encrypted by this script, and finalizing it
# would discard everything past a filesystem it may not even hold.
detect_state() {
  local type
  if ! cryptsetup isLuks "$device"; then
    type=$(blkid -o value -s TYPE "$device" || true)
    if [ "$type" != ext4 ]; then
      echo "yolobox: ${device} is ${type:-blank}, not ext4 or LUKS; refusing to touch it" >&2
      exit 3
    fi
    state=encrypt
    return 0
  fi
  # Captured first: `cmd | grep -q` under pipefail is a trap.
  dump=$(cryptsetup luksDump "$device")
  if [ "$(header_field Label)" = "$label" ]; then
    # Quoted because shellcheck reads a bare `done` after `=` as the keyword (SC1010).
    state="done"
  elif [ "$(header_field Subsystem)" != "$label" ]; then
    echo "yolobox: ${device} is LUKS but not marked as a yolobox migration (LUKS2 subsystem '${label}'); refusing to touch it" >&2
    exit 3
  elif grep -q '^Requirements:.*online-reencrypt' <<<"$dump"; then
    state=resume
    disk_state="partly encrypted; the next boot resumes the encryption"
  else
    state=finalize
    disk_state="encrypted but not finalized; the next boot finishes it"
  fi
}

encrypt() {
  local min bs target region total need
  echo "yolobox-luks-migrate: encrypt ${device}"
  fsck_ok "$device"
  min=$(resize2fs -P "$device" | awk '{print $NF}')
  require_positive_int "filesystem minimum size" "$min" "${device} was not shrunk or encrypted"
  bs=$(fs_block_size "$device")
  require_positive_int "filesystem block size" "$bs" "${device} was not shrunk or encrypted"
  target=$((min + min * headroom_percent / 100 + 1))
  # The shrunken filesystem plus the LUKS header room, rounded up to the
  # alignment. The encryption sector size stays at 512 bytes: the partition
  # length is not a multiple of 4096 and a 4096-byte sector would fail once the
  # segment covers the whole device.
  region=$(((target * bs + header_room + align - 1) / align * align))
  total=$(blockdev --getsize64 "$device")
  require_positive_int "device size" "$total" "${device} was not shrunk or encrypted"
  need=$((region + header_room))
  if [ "$need" -gt "$total" ]; then
    {
      echo "yolobox: ${device} is too full to encrypt in place: the shrunken filesystem plus LUKS headers need ${need} bytes of ${total}."
      echo "The box stays in its initrd until space is freed by hand: ssh in as root with the command yo printed above (docs/disk-encryption.md#recognising-failures),"
      echo "mount ${device} on /sysroot, delete build output, umount, then:"
      echo "  systemctl reset-failed yolobox-luks-migrate; systemctl start --no-block systemd-cryptsetup@${mapper}.service"
      echo "and run yo up again on the Mac."
    } >&2
    exit 7
  fi
  resize2fs "$device" "$target"
  disk_state="partly encrypted; the next boot resumes the encryption"
  # --subsystem marks the header as this script's work from its first byte;
  # the label comes only with finalize (detect_state reads both).
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency "$progress_seconds" \
    --encrypt --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id --subsystem "$label" \
    --reduce-device-size "${header_room_mib}M" --device-size "$region" --key-file - "$device"
  disk_state="encrypted but not finalized; the next boot finishes it"
  echo "yolobox-luks-migrate: encrypt done"
}

resume() {
  echo "yolobox-luks-migrate: resume ${device}"
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency "$progress_seconds" \
    --resume-only --key-file - "$device"
  disk_state="encrypted but not finalized; the next boot finishes it"
  echo "yolobox-luks-migrate: resume done"
}

# Discard is advisory: a backend that ignores it (qemu's default
# discard=ignore) keeps the plaintext in the image file. So the tail is zeroed
# first, then discarded, which gives the image its holes back where discard
# works. The zeroing goes through the disk's write-zeroes offload, where the
# host turns it into metadata; without the offload the kernel would write every
# zero by hand and the image file would grow by the whole tail, so only the
# discard runs and the journal says so.
clear_plaintext() {
  local offset=$1 length=$2 max
  max=$(cat "${sysfs_block}/$(basename "$device")/../queue/write_zeroes_max_bytes" 2>/dev/null || true)
  case "$max" in
    '' | *[!0-9]* | 0)
      echo "yolobox-luks-migrate: ${device} offloads no write-zeroes (${max:-unknown}); the old plaintext is only discarded, which a host that ignores discard keeps"
      ;;
    *) blkdiscard -z -f --offset "$offset" --length "$length" "$device" ;;
  esac
  blkdiscard -f --offset "$offset" --length "$length" "$device"
}

# Finalize opens the partition under a private name, never `$mapper`. The
# initrd's systemd starts its fsck and sysroot.mount the moment
# /dev/mapper/<mapper> exists, so opening the real name here would let them race
# the e2fsck and resize2fs below for the device (docs/disk-encryption.md,
# "The root mount raced the finalize"). Only systemd-cryptsetup opens `$mapper`.
finalize() {
  local rc scratch="${mapper}-finalize"
  echo "yolobox-luks-migrate: finalize ${device}"
  printf '%s' "$key" | cryptsetup open --allow-discards --key-file - "$device" "$scratch"
  # `( ... ) || rc=$?` would run the subshell with errexit off.
  set +e
  (
    set -euo pipefail
    offset_sectors=$(cryptsetup status "$scratch" | awk '/offset:/ {print $2}')
    require_positive_int offset_sectors "$offset_sectors" "nothing was discarded on ${device}"
    mapper_bs=$(fs_block_size "/dev/mapper/${scratch}")
    require_positive_int "filesystem block size" "$mapper_bs" "nothing was discarded on ${device}"
    fs_blocks=$(fs_block_count "/dev/mapper/${scratch}")
    require_positive_int "filesystem block count" "$fs_blocks" "nothing was discarded on ${device}"
    fs_bytes=$((fs_blocks * mapper_bs))
    plaintext_tail=$((offset_sectors * sector_bytes + fs_bytes))
    total=$(blockdev --getsize64 "$device")
    # The never-shrunk tail and reencrypt's temporary copy still hold plaintext.
    if [ "$plaintext_tail" -lt "$total" ]; then
      clear_plaintext "$plaintext_tail" $((total - plaintext_tail))
    fi
    fsck_ok "/dev/mapper/${scratch}"
    resize2fs "/dev/mapper/${scratch}"
  )
  rc=$?
  set -e
  # Closed in every case: systemd-cryptsetup cannot open the device while the
  # scratch mapping holds it. A close that fails is reported with whatever
  # failed before it, not in place of it.
  if ! cryptsetup close "$scratch"; then
    echo "yolobox: cryptsetup close ${scratch} failed; ${device} is still held by that mapping" >&2
    rc=5
  fi
  if [ "$rc" -ne 0 ]; then
    echo "yolobox: finalize failed (exit $rc), ${device} left unlabelled" >&2
    exit 5
  fi
  cryptsetup config --label "$label" "$device"
  echo "yolobox-luks-migrate: finalize done"
}

detect_state
case "$state" in
  done) exit 0 ;;
  encrypt) read_key trust; encrypt; finalize ;;
  resume) read_key verify; resume; finalize ;;
  finalize) read_key verify; finalize ;;
  *) echo "yolobox: unknown state '$state'" >&2; exit 6 ;;
esac
