# shellcheck shell=bash
# Runs in the initrd as yolobox-luks-migrate.service, before the root device is
# opened: encrypts the plain ext4 root in place, resumes an interrupted
# encryption, or finishes one, depending on what it finds. A disk that already
# carries the label is left alone and nothing is asked. See nix/luks.nix.
if [ "$#" -ne 2 ]; then
  echo "usage: yolobox-luks-migrate <device> <label>" >&2
  exit 2
fi
device=$1
label=$2

mib=$((1024 * 1024))

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
  target=$((min + min / 20 + 1))
  # The shrunken filesystem plus the 32 MiB LUKS header room, rounded up to
  # 4 MiB. The encryption sector size stays at 512 bytes: the partition length
  # is not a multiple of 4096 and a 4096-byte sector would fail once the
  # segment covers the whole device.
  region=$(((target * bs + 32 * mib + 4 * mib - 1) / (4 * mib) * (4 * mib)))
  total=$(blockdev --getsize64 "$device")
  need=$((region + 32 * mib))
  if [ "$need" -gt "$total" ]; then
    {
      echo "yolobox: ${device} is too full to encrypt in place: the shrunken filesystem plus LUKS headers need ${need} bytes of ${total}."
      echo "The box stays in its initrd until space is freed by hand: ssh in as root on port 60022 (docs/disk-encryption.md#recognising-failures),"
      echo "mount ${device} on /sysroot, delete build output, umount, then:"
      echo "  systemctl reset-failed yolobox-luks-migrate; systemctl start --no-block systemd-cryptsetup@root.service"
      echo "and run yo up again on the Mac."
    } >&2
    exit 7
  fi
  resize2fs "$device" "$target"
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency 30 \
    --encrypt --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id \
    --reduce-device-size 32M --device-size "$region" --key-file - "$device"
  echo "yolobox-luks-migrate: encrypt done"
}

resume() {
  echo "yolobox-luks-migrate: resume ${device}"
  printf '%s' "$key" | cryptsetup reencrypt --batch-mode --progress-frequency 30 \
    --resume-only --key-file - "$device"
  echo "yolobox-luks-migrate: resume done"
}

finalize() {
  local rc
  echo "yolobox-luks-migrate: finalize ${device}"
  printf '%s' "$key" | cryptsetup open --allow-discards --key-file - "$device" root
  # `( ... ) || rc=$?` would run the subshell with errexit off.
  set +e
  (
    set -euo pipefail
    offset_sectors=$(cryptsetup status root | awk '/offset:/ {print $2}')
    mapper_bs=$(fs_block_size /dev/mapper/root)
    fs_bytes=$(($(fs_block_count /dev/mapper/root) * mapper_bs))
    tail=$((offset_sectors * 512 + fs_bytes))
    total=$(blockdev --getsize64 "$device")
    # The never-shrunk tail and reencrypt's temporary copy still hold plaintext.
    if [ "$tail" -lt "$total" ]; then
      blkdiscard -f --offset "$tail" --length $((total - tail)) "$device"
    fi
    fsck_ok /dev/mapper/root
    resize2fs /dev/mapper/root
  )
  rc=$?
  set -e
  # Closed in every case: systemd-cryptsetup would report a half-finalized
  # device as "already active".
  cryptsetup close root
  if [ "$rc" -ne 0 ]; then
    echo "yolobox: finalize failed (exit $rc), ${device} left closed" >&2
    exit 5
  fi
  cryptsetup config --label "$label" "$device"
  echo "yolobox-luks-migrate: finalize done"
}

detect_state
case "$state" in
  "done") exit 0 ;;
  encrypt) read_key; encrypt; finalize ;;
  resume) read_key; resume; finalize ;;
  finalize) read_key; finalize ;;
  *) echo "yolobox: unknown state '$state'" >&2; exit 6 ;;
esac
