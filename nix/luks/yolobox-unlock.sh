# shellcheck shell=bash
# Runs in the initrd over ssh, started by `yo up`: reads the passphrase on
# stdin, answers every systemd password ask with it, and reports how the
# unlock ended through its exit code. Event-driven, no polling. See nix/luks.nix.
#   exit 0  the cryptsetup unit is active
#   exit 1  the cryptsetup unit failed (the passphrase does not open the disk)
#   exit 2  the migration unit failed
#   exit 3  the ask-password watch could not be set up or ended on its own
#   exit 4  no passphrase on stdin
shopt -s nullglob

UNTIL_UNIT="${YOLOBOX_UNLOCK_UNTIL_UNIT:-systemd-cryptsetup@root.service}"
MIGRATE_UNIT="${YOLOBOX_UNLOCK_MIGRATE_UNIT:-yolobox-luks-migrate.service}"
ASK_DIR="${YOLOBOX_ASK_DIR:-/run/systemd/ask-password}"

IFS= read -r key || true
if [ -z "$key" ]; then
  echo "yolobox-unlock: no passphrase on stdin" >&2
  exit 4
fi

# Newline-delimited set of ask files already answered, keyed by full path. A
# string, not an associative array, so the tests also run under macOS bash 3.2.
answered=$'\n'

answer_pending() {
  local f socket id
  for f in "$ASK_DIR"/ask.*; do
    if [ ! -e "$f" ]; then
      continue
    fi
    case "$answered" in *$'\n'"$f"$'\n'*) continue ;; esac
    socket=$(sed -n 's/^Socket=//p' "$f")
    id=$(sed -n 's/^Id=//p' "$f")
    if [ -z "$socket" ]; then
      echo "yolobox-unlock: ${f} names no socket, skipped" >&2
      continue
    fi
    if ! printf '%s' "$key" | "$REPLY_PASSWORD" 1 "$socket"; then
      echo "yolobox-unlock: reply to ${id} failed" >&2
      continue
    fi
    answered="${answered}${f}"$'\n'
    echo "yolobox-unlock: answered id=${id}"
  done
}

check_units() {
  if systemctl is-failed --quiet "$MIGRATE_UNIT"; then
    journalctl -u "$MIGRATE_UNIT" -o cat --no-pager | tail -n 20 >&2
    echo "yolobox-unlock: encrypting ${LUKS_DEVICE} failed; its journal is above" >&2
    exit 2
  fi
  if systemctl is-active --quiet "$UNTIL_UNIT"; then
    echo "yolobox-unlock: unlocked"
    exit 0
  fi
  if systemctl is-failed --quiet "$UNTIL_UNIT"; then
    echo "yolobox-unlock: the passphrase does not open ${LUKS_DEVICE}" >&2
    exit 1
  fi
  return 0
}

# Two plain background writers into a FIFO, never a pipeline: a pipeline's `$!`
# is its last member, and a surviving first member would keep the ssh session's
# stderr open after this script exits. Opening a FIFO for writing blocks until
# the loop below opens the read end, so neither starts before it.
fifo=$(mktemp -u /tmp/yolobox-unlock.XXXXXX)
mkfifo "$fifo"
trap 'kill "${inotify_pid:-}" "${journal_pid:-}" 2>/dev/null || true; rm -f "$fifo"' EXIT

# Only asks are watched, not /dev/mapper: a unit changing state posts no file
# event whatever the mapper does, and the journal below carries unit state.
# systemd creates the directory itself; creating it here only keeps the watch
# from failing when the helper runs before systemd's first ask.
mkdir -p "$ASK_DIR" || {
  echo "yolobox-unlock: cannot create ${ASK_DIR}" >&2
  exit 3
}
# No -q: the two startup lines are the first events, so the first scan runs
# after the watch exists and nothing created in between is missed.
inotifywait -m -e create -e close_write -e moved_to -e delete \
  --format 'inotify %e %w%f' "$ASK_DIR" > "$fifo" 2>&1 &
inotify_pid=$!
# A unit changing state posts no file event; its journal lines wake the loop.
# -n 20 replays the last lines on purpose: a state change logged just before
# journalctl attached still produces an event.
journalctl -f -n 20 -q -o cat -u "$MIGRATE_UNIT" -u "$UNTIL_UNIT" > "$fifo" 2>/dev/null &
journal_pid=$!

# Fed from a FIFO, not a pipe, so the loop runs in the main shell and `exit`
# inside check_units ends the script.
while IFS= read -r line; do
  case "$line" in
    # Without the watch a new ask would go unanswered and the helper would wait
    # on the journal alone.
    "Couldn't watch "* | "Failed to watch "*)
      echo "yolobox-unlock: ${line}" >&2
      exit 3
      ;;
    "inotify "* | "Setting up watches." | "Watches established.") ;;
    *) echo "$line" ;;
  esac
  answer_pending
  check_units
done < "$fifo"

echo "yolobox-unlock: lost the ask-password watch" >&2
exit 3
