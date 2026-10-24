# shellcheck shell=bash
# Runs in the initrd as yolobox-luks-migrate.service, before the root device is
# opened: encrypts the plain ext4 root in place, resumes an interrupted
# encryption, or finishes one, depending on what it finds. A disk that already
# carries the label is left alone and nothing is asked. See nix/luks.nix.
# YOLOBOX_SSH_LOCAL_PORT (the Mac-side port of the initrd sshd) comes from
# nix/luks/facts.nix through apps.nix.
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

read_key() {
  if [ -n "${YOLOBOX_LUKS_KEY_FILE:-}" ]; then
    # Rehearsal only; the initrd never sets this.
    key=$(cat "$YOLOBOX_LUKS_KEY_FILE")
  else
    key=$(systemd-ask-password --no-tty --timeout=0 --id=yolobox:luks-migrate "yolobox: passphrase to encrypt ${device} in place")
  fi
  if [ -z "$key" ]; then
    echo "yolobox: no passphrase given" >&2
    exit 2
  fi
}

fs_block_size() {
  dumpe2fs -h "$1" 2>/dev/null | awk -F: '/^Block size/ {gsub(/ /, "", $2); print $2}'
}

fs_block_count() {
  dumpe2fs -h "$1" 2>/dev/null | awk -F: '/^Block count/ {gsub(/ /, "", $2); print $2}'
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

# Sets the global `state`. Called plainly, never inside $(...), so its exit
# ends the script.
detect_state() {
  local type dump
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
  if grep -q '^Requirements:.*online-reencrypt' <<<"$dump"; then
    state=resume
  elif [ "$(blkid -o value -s LABEL "$device")" = "$label" ]; then
    # Quoted because shellcheck reads a bare `done` after `=` as the keyword (SC1010).
    state="done"
  else
    state=finalize
  fi
}

encrypt() {
  local min bs target region total need
  echo "yolobox-luks-migrate: encrypt ${device}"
  fsck_ok "$device"
  min=$(resize2fs -P "$device" | awk '{print $NF}')
  bs=$(fs_block_size "$device")
  target=$((min + min * headroom_percent / 100 + 1))
  # The shrunken filesystem plus the LUKS header room, rounded up to the
  # alignment. The encryption sector size stays at 512 bytes: the partition
  # length is not a multiple of 4096 and a 4096-byte sector would fail once the
  # segment covers the whole device.
  region=$(((target * bs + header_room + align - 1) / align * align))
  total=$(blockdev --getsize64 "$device")
  need=$((region + header_room))
  if [ "$need" -gt "$total" ]; then
    {
      echo "yolobox: ${device} is too full to encrypt in place: the shrunken filesystem plus LUKS headers need ${need} bytes of ${total}."
      echo "The box stays in its initrd until space is freed by hand: ssh in as root on port ${YOLOBOX_SSH_LOCAL_PORT} (docs/disk-encryption.md#recognising-failures),"
      echo "mount ${device} on /sysroot, delete build output, umount, then:"
      echo "  systemctl reset-failed yolobox-luks-migrate; systemctl start --no-block systemd-cryptsetup@${mapper}.service"
      echo "and run yo up again on the Mac."
    } >&2
    exit 7
  fi
  resize2fs "$device" "$target"
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency "$progress_seconds" \
    --encrypt --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id \
    --reduce-device-size "${header_room_mib}M" --device-size "$region" --key-file - "$device"
  echo "yolobox-luks-migrate: encrypt done"
}

resume() {
  echo "yolobox-luks-migrate: resume ${device}"
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency "$progress_seconds" \
    --resume-only --key-file - "$device"
  echo "yolobox-luks-migrate: resume done"
}

finalize() {
  local rc
  echo "yolobox-luks-migrate: finalize ${device}"
  printf '%s' "$key" | cryptsetup open --allow-discards --key-file - "$device" "$mapper"
  # `( ... ) || rc=$?` would run the subshell with errexit off.
  set +e
  (
    set -euo pipefail
    offset_sectors=$(cryptsetup status "$mapper" | awk '/offset:/ {print $2}')
    mapper_bs=$(fs_block_size "/dev/mapper/${mapper}")
    fs_bytes=$(($(fs_block_count "/dev/mapper/${mapper}") * mapper_bs))
    plaintext_tail=$((offset_sectors * sector_bytes + fs_bytes))
    total=$(blockdev --getsize64 "$device")
    # The never-shrunk tail and reencrypt's temporary copy still hold plaintext.
    if [ "$plaintext_tail" -lt "$total" ]; then
      blkdiscard -f --offset "$plaintext_tail" --length $((total - plaintext_tail)) "$device"
    fi
    fsck_ok "/dev/mapper/${mapper}"
    resize2fs "/dev/mapper/${mapper}"
  )
  rc=$?
  set -e
  # Closed in every case: systemd-cryptsetup would report a half-finalized
  # device as "already active".
  cryptsetup close "$mapper"
  if [ "$rc" -ne 0 ]; then
    echo "yolobox: finalize failed (exit $rc), ${device} left closed" >&2
    exit 5
  fi
  cryptsetup config --label "$label" "$device"
  echo "yolobox-luks-migrate: finalize done"
}

detect_state
case "$state" in
  done) exit 0 ;;
  encrypt) read_key; encrypt; finalize ;;
  resume) read_key; resume; finalize ;;
  finalize) read_key; finalize ;;
  *) echo "yolobox: unknown state '$state'" >&2; exit 6 ;;
esac
