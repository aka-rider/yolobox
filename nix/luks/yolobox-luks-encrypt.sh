# shellcheck shell=bash
# Runs in the initrd as yolobox-luks-encrypt.service, before the root device is
# opened. A plain ext4 root (the seed image's first boot) is encrypted in place;
# a LUKS disk carrying the label is left alone and nothing is asked; any other
# disk is refused.
if [ "$#" -ne 3 ]; then
  echo "usage: yolobox-luks-encrypt <device> <label> <mapper>" >&2
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

# An interrupted or failed encryption is not resumed: the box is recreated.
recreate="the box must be recreated: yo destroy --yes && yo bootstrap"

report_failure() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "yolobox-luks-encrypt: failed (exit ${rc}); ${recreate}" >&2
  fi
}
trap report_failure EXIT

fs_block_size() {
  dumpe2fs -h "$1" | awk -F: '/^Block size/ {gsub(/ /, "", $2); print $2}'
}

fs_block_count() {
  dumpe2fs -h "$1" | awk -F: '/^Block count/ {gsub(/ /, "", $2); print $2}'
}

# A number read from a tool's output, checked before it is used in arithmetic:
# bash reads an empty or non-numeric string as 0, which would move the discard
# offset to the start of the device or collapse the region the encryption is
# limited to. $3 says what was left untouched.
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

# Exits 0 for a finished disk; returns for a plain ext4 one; refuses the rest.
# A LUKS disk without the label is an interrupted encryption or someone else's
# disk, and touching either could destroy what it holds.
check_disk() {
  local type
  if cryptsetup isLuks "$device"; then
    if cryptsetup luksDump "$device" | awk -F: -v label="$label" \
      '$1 == "Label" {sub(/^[[:space:]]+/, "", $2); if ($2 == label) found = 1} END {exit !found}'; then
      exit 0
    fi
    echo "yolobox: root disk ${device} is in an unexpected state (LUKS without the ${label} label); ${recreate}" >&2
    exit 3
  fi
  type=$(blkid -o value -s TYPE "$device" || true)
  if [ "$type" != ext4 ]; then
    echo "yolobox: root disk ${device} is in an unexpected state (${type:-blank}, not ext4 or LUKS); ${recreate}" >&2
    exit 3
  fi
}

# Zeroes the tail through the disk's write-zeroes offload, then discards it;
# without the offload only the discard runs, because zeroing would grow the
# image file by the whole tail.
clear_plaintext() {
  local offset=$1 length=$2 max
  max=$(cat "${sysfs_block}/$(basename "$device")/../queue/write_zeroes_max_bytes" 2>/dev/null || true)
  case "$max" in
    '' | *[!0-9]* | 0)
      echo "yolobox-luks-encrypt: ${device} offloads no write-zeroes (${max:-unknown}); the old plaintext is only discarded, which a host that ignores discard keeps"
      ;;
    *) blkdiscard -z -f --offset "$offset" --length "$length" "$device" ;;
  esac
  blkdiscard -f --offset "$offset" --length "$length" "$device"
}

# The partition is opened under a private name, never `$mapper`: the initrd's
# systemd starts its fsck and sysroot.mount the moment /dev/mapper/<mapper>
# exists, which raced the e2fsck and resize2fs below for the device.
finalize() {
  local rc scratch="${mapper}-finalize"
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
    echo "yolobox: growing the filesystem into ${device} failed (exit $rc), ${device} left unlabelled" >&2
    exit 5
  fi
}

check_disk
echo "yolobox-luks-encrypt: encrypt ${device}"
# A plain ext4 device has nothing to verify a passphrase against, so the first
# answer is the key.
key=$(systemd-ask-password --no-tty --timeout=0 --id=yolobox:luks-encrypt "yolobox: passphrase to encrypt ${device} in place")
if [ -z "$key" ]; then
  echo "yolobox: no passphrase given" >&2
  exit 2
fi
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
  echo "yolobox: ${device} is too full to encrypt in place: the shrunken filesystem plus LUKS headers need ${need} bytes of ${total}; ${device} is still plaintext" >&2
  exit 7
fi
resize2fs "$device" "$target"
# --device-size limits the rewrite to the shrunken filesystem; the tail past it
# is cleared afterwards (clear_plaintext). The passphrase goes in without a
# trailing newline: a key file is taken byte for byte.
printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency "$progress_seconds" \
  --encrypt --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id \
  --reduce-device-size "${header_room_mib}M" --device-size "$region" --key-file - "$device"
finalize
# The label is the last write: seeing it means the disk is done.
cryptsetup config --label "$label" "$device"
echo "yolobox-luks-encrypt: encrypt done"
