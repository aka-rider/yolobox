# shellcheck shell=bash
# Runs in the initrd over ssh, started by `yo up`: reads the passphrase on
# stdin, answers systemd's password asks with it, and reports how the unlock
# ended through its exit code. Exit 0, with YOLOBOX_UNLOCK_UNLOCKED_LINE (the
# fact unlock_unlocked_line) as the last line on stdout, is an open disk with
# its root filesystem mounted. The failure codes are facts.json's unlock_exit_* values, passed in
# as YOLOBOX_UNLOCK_EXIT_*:
#   wrong_key       the passphrase does not open the disk; an asker rejected its reply
#   encrypt_failed  the in-place encryption failed; its journal tail is on stderr
#   watch_failed    the ask-password watch failed to start or ended on its own
#   no_passphrase   no passphrase on stdin
#   mount_failed    the disk opened but the root mount failed or needed a retry
#   unexpected      anything else failed; stderr names the command, not bash's code
#   open_failed     the open failed for a reason other than the key; journal on stderr
shopt -s nullglob
# inotifywait's and the journal's words are read below; a translated tool would
# say something else.
export LC_ALL=C
EXIT_WRONG_KEY="${YOLOBOX_UNLOCK_EXIT_WRONG_KEY:?}"
EXIT_ENCRYPT_FAILED="${YOLOBOX_UNLOCK_EXIT_ENCRYPT_FAILED:?}"
EXIT_WATCH_FAILED="${YOLOBOX_UNLOCK_EXIT_WATCH_FAILED:?}"
EXIT_NO_PASSPHRASE="${YOLOBOX_UNLOCK_EXIT_NO_PASSPHRASE:?}"
EXIT_MOUNT_FAILED="${YOLOBOX_UNLOCK_EXIT_MOUNT_FAILED:?}"
EXIT_UNEXPECTED="${YOLOBOX_UNLOCK_EXIT_UNEXPECTED:?}"
EXIT_OPEN_FAILED="${YOLOBOX_UNLOCK_EXIT_OPEN_FAILED:?}"
LUKS_DEVICE="${YOLOBOX_UNLOCK_LUKS_DEVICE:?}"
REPLY_PASSWORD="${YOLOBOX_UNLOCK_REPLY_PASSWORD:?}"
UNLOCKED_LINE="${YOLOBOX_UNLOCK_UNLOCKED_LINE:?}"
# -E carries the ERR trap into functions, where most commands run.
set -E
trap 'echo "yolobox-unlock: unexpected failure (exit $?) at line ${LINENO}: ${BASH_COMMAND}" >&2; exit "$EXIT_UNEXPECTED"' ERR

# The unit that opens the disk and the one that encrypts it first on the
# seed's first boot.
UNIT="${YOLOBOX_UNLOCK_UNIT:?}"
ENCRYPT_UNIT="${YOLOBOX_UNLOCK_ENCRYPT_UNIT:?}"
ROOT_MOUNT_UNIT=sysroot.mount
# What the initrd reaches when a required mount fails; nothing leaves it.
EMERGENCY_UNIT=emergency.target
ASK_DIR="${YOLOBOX_ASK_DIR:?}"
journal_tail_lines=20
# The ask-password watch and the blocking start of the cryptsetup unit, while
# they run: left behind, either would hold the ssh session open after the
# helper exits. The start gets SIGKILL: bash loses a TERM that lands between
# its fork and the exec of systemctl.
watch_pid=
start_pid=
trap 'kill "$watch_pid" 2>/dev/null || true; kill -KILL "$start_pid" 2>/dev/null || true' EXIT

IFS= read -r key || true
if [ -z "$key" ]; then
  echo "yolobox-unlock: no passphrase on stdin" >&2
  exit "$EXIT_NO_PASSPHRASE"
fi

# The ask files already answered: systemd removes one only some time after its
# answer, and a second reply to it would meet a closed socket. And their ids:
# an asker that posts an answered id again has rejected the answer.
declare -A replied=() replied_ids=()

# Answers every ask file present that is not answered yet. systemd removes an
# ask the moment it is answered or withdrawn, so a file can vanish between the
# glob and the read, or before the reply: it is skipped, and the next event
# scans again.
reply_pending() {
  local f socket id
  for f in "$ASK_DIR"/ask.*; do
    [ -z "${replied[$f]:-}" ] || continue
    if ! socket=$(sed -n 's/^Socket=//p' "$f" 2>/dev/null) || ! id=$(sed -n 's/^Id=//p' "$f" 2>/dev/null); then
      echo "yolobox-unlock: ${f} could not be read, skipped" >&2
      continue
    fi
    if [ -n "${replied_ids[$id]:-}" ]; then
      echo "yolobox-unlock: the passphrase does not open ${LUKS_DEVICE}: ${id} asked again after it was answered" >&2
      exit "$EXIT_WRONG_KEY"
    fi
    if ! printf '%s' "$key" | "$REPLY_PASSWORD" 1 "$socket"; then
      if [ ! -e "$f" ]; then
        echo "yolobox-unlock: ${id} was withdrawn before the reply, skipped" >&2
        continue
      fi
      echo "yolobox-unlock: reply to ${id} failed" >&2
      exit "$EXIT_UNEXPECTED"
    fi
    replied[$f]=1
    replied_ids[$id]=1
    echo "yolobox-unlock: answered id=${id}"
  done
}

# Answers every ask posted, each once, for as long as it runs; it ends only
# on a failure: watch_failed when the watch cannot be set up or ends,
# unexpected when a reply fails. The watch is set before the first scan: an ask posted between a scan
# and the watch would go unanswered. No -q: the startup lines are the first
# events, so the first scan runs once the watch exists.
watch_asks() {
  local line inotify_pid
  exec 3< <(inotifywait -m -e create -e moved_to --format event "$ASK_DIR" 2>&1)
  inotify_pid=$!
  trap 'kill "$inotify_pid" 2>/dev/null || true' EXIT
  while IFS= read -r line <&3; do
    case "$line" in
      "Setting up watches.") continue ;;
      "Watches established." | event) reply_pending ;;
      *)
        echo "yolobox-unlock: ${line}" >&2
        exit "$EXIT_WATCH_FAILED"
        ;;
    esac
  done
  echo "yolobox-unlock: the ask-password watch ended" >&2
  exit "$EXIT_WATCH_FAILED"
}

# Runs the watch beside the unit's blocking start, which posts the asks (the
# encryption's on the seed's first boot, then the open's), and returns the
# start's exit code, the verdict. Whichever ends first decides: a start that
# ends before any ask is judged at once, where a watch alone would wait
# forever, and a watch that fails ends the helper with its own code.
answer_asks_until_started() {
  local ended='' rc=0
  watch_asks &
  watch_pid=$!
  wait -n -p ended "$watch_pid" "$start_pid" || rc=$?
  if [ "$ended" = "$watch_pid" ]; then
    watch_pid=
    exit "$rc"
  fi
  start_pid=
  kill "$watch_pid" 2>/dev/null || true
  watch_pid=
  return "$rc"
}

journal_tail() {
  journalctl -u "$1" -o cat --no-pager | tail -n "$journal_tail_lines" >&2
}

# What the open logged since the cursor taken before its start: an earlier
# attempt's wrong key is not this attempt's verdict.
open_journal() {
  journalctl -u "$UNIT" --after-cursor="$open_cursor" -o cat --no-pager
}

# systemd creates the directory itself; creating it here only keeps the watch
# from failing when the helper runs before systemd's first ask.
mkdir -p "$ASK_DIR"

# A failed encryption never asks again, and nothing here may run it again:
# the box is recreated.
if systemctl is-failed --quiet "$ENCRYPT_UNIT"; then
  journal_tail "$ENCRYPT_UNIT"
  echo "yolobox-unlock: the in-place encryption of ${LUKS_DEVICE} failed" >&2
  exit "$EXIT_ENCRYPT_FAILED"
fi

if ! systemctl is-active --quiet "$UNIT"; then
  # A wrong passphrase on an earlier `yo up` left the unit failed (tries=1);
  # a unit whose job already exists is left to it.
  systemctl reset-failed "$UNIT"
  open_cursor=$(journalctl -n 1 --show-cursor -o cat --no-pager | sed -n 's/^-- cursor: //p')
  if [ -z "$open_cursor" ]; then
    echo "yolobox-unlock: the journal named no cursor to read the open's lines after" >&2
    exit "$EXIT_UNEXPECTED"
  fi
  # Blocks until the open ends, and on the seed's first boot until the
  # encryption the open requires ends before it; each posts its own ask.
  systemctl start "$UNIT" &
  start_pid=$!
  start_rc=0
  answer_asks_until_started || start_rc=$?
  if [ "$start_rc" != 0 ]; then
    if systemctl is-failed --quiet "$ENCRYPT_UNIT"; then
      journal_tail "$ENCRYPT_UNIT"
      echo "yolobox-unlock: the in-place encryption of ${LUKS_DEVICE} failed" >&2
      exit "$EXIT_ENCRYPT_FAILED"
    fi
    # systemd-cryptsetup's own words for a key that does not open the disk.
    if open_journal | grep -F 'Passphrase incorrect?' >/dev/null; then
      echo "yolobox-unlock: the passphrase does not open ${LUKS_DEVICE}" >&2
      exit "$EXIT_WRONG_KEY"
    fi
    open_journal | tail -n "$journal_tail_lines" >&2
    echo "yolobox-unlock: unlock failed (exit ${start_rc})" >&2
    exit "$EXIT_OPEN_FAILED"
  fi
fi

# An open disk is not a booted one: systemd still fscks and mounts it. The job
# already exists, so this joins it. A mount that had already failed is retried
# by start, and a retry that works is no success: initrd.target is not
# restarted, so the box stays in emergency mode.
if ! systemctl start "$ROOT_MOUNT_UNIT"; then
  journal_tail "$ROOT_MOUNT_UNIT"
  echo "yolobox-unlock: ${LUKS_DEVICE} opened but ${ROOT_MOUNT_UNIT} failed; the box waits in its initrd emergency mode" >&2
  exit "$EXIT_MOUNT_FAILED"
fi
if systemctl is-active --quiet "$EMERGENCY_UNIT"; then
  journal_tail "$ROOT_MOUNT_UNIT"
  echo "yolobox-unlock: ${ROOT_MOUNT_UNIT} mounted only on retry and the initrd is in emergency mode (${EMERGENCY_UNIT}), so the boot will not continue" >&2
  exit "$EXIT_MOUNT_FAILED"
fi
echo "${UNLOCKED_LINE}"
