#!/usr/bin/env bash
# Records the virtual display to an mp4 under ~/artifacts. Built by
# nix/display.nix with writeShellApplication, which prepends
# `set -o errexit -o nounset -o pipefail` and puts the `runtimeInputs` listed
# there on PATH.

# State lives under ~/.local/state, not /run/user: /run/user is torn
# down at last logout while a backgrounded ffmpeg keeps running,
# orphaning an unfinalized mp4 with no way to stop it cleanly.
state_dir="$HOME/.local/state/yolobox"
# key=value lines: the recording's pid, its identity (below), its wrapper's
# pid and its output file.
pid_file="$state_dir/screen-record.pid"
# ffmpeg's exit status, written by its wrapper once ffmpeg is gone.
status_file="$state_dir/screen-record.status"
lock_file="$state_dir/screen-record.lock"
progress_fifo="$state_dir/screen-record.progress"
verdict_fifo="$state_dir/screen-record.verdict"
# Seconds the wrapper waits for ffmpeg's first progress report, start waits
# for the wrapper's verdict on top of that, stop waits for ffmpeg to finalize
# the mp4 after SIGINT, and either waits for the other to release the lock.
start_timeout=10
verdict_grace=5
stop_timeout=20
lock_timeout=5

usage() {
  echo "usage: yolobox-screen-record {start|stop}" >&2
  exit 1
}

# One start or stop at a time: the check "is a recording live" and the pid
# file it decides on must not interleave with another's.
take_lock() {
  exec 9>"$lock_file"
  if ! flock -w "$lock_timeout" 9; then
    echo "yolobox-screen-record: another start or stop holds $lock_file" >&2
    exit 1
  fi
}

# A process identity that survives pid reuse: the pid and its start time (in
# clock ticks since boot, field 22 of /proc/<pid>/stat) in one boot (boot_id).
# The pid file outlives a reboot and the kernel hands the pid to some other
# process by then, so a live pid alone proves nothing. Prints the start time
# of a live pid, nothing for a gone one.
starttime_of() {
  local stat rest
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 0
  # comm (field 2) may hold spaces and parentheses; the fields after it do not.
  rest="${stat##*) }"
  # shellcheck disable=SC2086
  set -- $rest
  echo "${20}"
}

boot_id() {
  cat /proc/sys/kernel/random/boot_id
}

# Sets rec_pid, rec_start, rec_boot, rec_wrapper and rec_out from the pid
# file, with a line not of the form key=value ignored. Fails when there is no
# pid file.
read_pid_file() {
  local line
  rec_pid='' rec_start='' rec_boot='' rec_wrapper='' rec_out=''
  [ -f "$pid_file" ] || return 1
  while IFS= read -r line; do
    case "$line" in
      pid=*) rec_pid="${line#pid=}" ;;
      start=*) rec_start="${line#start=}" ;;
      boot=*) rec_boot="${line#boot=}" ;;
      wrapper=*) rec_wrapper="${line#wrapper=}" ;;
      out=*) rec_out="${line#out=}" ;;
    esac
  done <"$pid_file"
}

# Succeeds when the pid file names a recording that is still the process it
# was: same boot, same pid, same start time. A pid file that does not is stale
# and is removed. Fails with nothing to say when there is no pid file.
recorder_is_live() {
  read_pid_file || return 1
  case "$rec_pid" in
    '' | *[!0-9]*) ;;
    *)
      if [ "$rec_boot" = "$(boot_id)" ] && [ -n "$rec_start" ] \
        && [ "$(starttime_of "$rec_pid")" = "$rec_start" ]; then
        return 0
      fi
      ;;
  esac
  rm -f "$pid_file"
  return 1
}

# Reads fd 4 up to the end of ffmpeg's first progress block, which ffmpeg
# writes once it is recording. Returns 0 when the block arrives, 1 when ffmpeg
# closed the pipe first, which it does when it exits, and 2 on timeout.
await_first_progress() {
  local line status
  while :; do
    status=0
    read -r -t "$start_timeout" line <&4 || status=$?
    if ((status > 128)); then return 2; fi
    if ((status != 0)); then return 1; fi
    if [ "$line" = progress=continue ]; then return 0; fi
  done
}

# ffmpeg's parent for the whole recording, so that its exit status is known:
# `stop` is not its parent and can only learn that it exited. Runs in the
# background with start's stdout, stderr and lock closed, so that start
# returns as soon as the verdict is in, and with its stderr on the
# recording's log, which ffmpeg inherits: the log is opened once, by start,
# and no child has a log redirection to fail at. Reports one verdict line to
# the verdict fifo: `recording <pid> <starttime>`, `exited <status>` or
# `timeout`, and after ffmpeg exits writes its status to $status_file.
record() {
  local out="$1" pid starttime ready code
  ffmpeg -nostdin -loglevel error -progress pipe:3 -f x11grab \
    -video_size 1920x1080 -framerate 15 -i "${DISPLAY:-:0}" -c:v libx264 \
    -preset ultrafast -pix_fmt yuv420p "$out" \
    3>"$progress_fifo" >/dev/null &
  pid=$!
  # Read before anything else: bash reaps a child the moment it exits, and
  # /proc goes with it. Empty means ffmpeg is already gone; `wait` says why.
  starttime="$(starttime_of "$pid")"
  exec 4<"$progress_fifo"
  rm -f "$progress_fifo"

  ready=0
  await_first_progress || ready=$?
  case $ready in
    1)
      code=0
      wait "$pid" || code=$?
      verdict "exited $code"
      exit 0
      ;;
    2)
      # It may have exited since the read gave up; then there is nothing to kill.
      kill -KILL "$pid" 2>/dev/null || true
      verdict timeout
      exit 0
      ;;
  esac
  verdict "recording $pid $starttime"
  # Something must read the pipe until ffmpeg exits: ffmpeg survives writing
  # to a pipe no one reads, but logs it as an error when it finishes.
  cat <&4 >/dev/null
  code=0
  wait "$pid" || code=$?
  echo "$code" >"$status_file.tmp"
  mv "$status_file.tmp" "$status_file"
}

# `<>` so the open never waits for a reader: start may have given up and gone.
verdict() {
  printf '%s\n' "$1" 1<>"$verdict_fifo"
}

cmd_start() {
  local dir out log stamp n wrapper verdict pid starttime
  mkdir -p "$state_dir"
  take_lock
  if recorder_is_live; then
    echo "yolobox-screen-record: already recording (pid $rec_pid)" >&2
    exit 1
  fi

  dir="${YOLOBOX_ARTIFACTS:-$HOME/artifacts}"
  mkdir -p "$dir"
  stamp="$(date +%Y%m%d-%H%M%S)"
  out="$dir/screen-$stamp.mp4"
  n=1
  while [ -e "$out" ] || [ -e "${out%.mp4}.log" ]; do
    n=$((n + 1))
    out="$dir/screen-$stamp-$n.mp4"
  done
  log="${out%.mp4}.log"
  # Before ffmpeg: a directory ffmpeg cannot write to is reported here, not
  # found out from a child that died at its redirections.
  if ! : 2>/dev/null >"$log"; then
    echo "yolobox-screen-record: cannot write to $dir" >&2
    exit 1
  fi

  rm -f "$status_file" "$progress_fifo" "$verdict_fifo"
  mkfifo "$progress_fifo" "$verdict_fifo"
  exec 5<>"$verdict_fifo"
  # The wrapper gives up start's stdout and stderr, else a caller reading
  # them waits for the recording to end, and the lock, else stop cannot take it.
  record "$out" </dev/null >>"$log" 2>&1 5>&- 9>&- &
  wrapper=$!

  verdict=''
  if ! read -r -t "$((start_timeout + verdict_grace))" verdict <&5; then
    kill -KILL "$wrapper" 2>/dev/null || true
    echo "yolobox-screen-record: the recorder gave no verdict within $((start_timeout + verdict_grace))s, killed it; log follows:" >&2
    cat "$log" >&2
    exit 1
  fi
  rm -f "$verdict_fifo"
  case "$verdict" in
    exited\ *)
      echo "yolobox-screen-record: ffmpeg exited with status ${verdict#exited } before it started recording, log follows:" >&2
      cat "$log" >&2
      exit 1
      ;;
    timeout)
      echo "yolobox-screen-record: ffmpeg did not start recording within ${start_timeout}s, killed it; log follows:" >&2
      cat "$log" >&2
      exit 1
      ;;
    recording\ *)
      read -r _ pid starttime <<<"$verdict"
      ;;
    *)
      echo "yolobox-screen-record: the recorder answered '$verdict', which is no verdict" >&2
      exit 1
      ;;
  esac

  printf 'pid=%s\nstart=%s\nboot=%s\nwrapper=%s\nout=%s\n' \
    "$pid" "$starttime" "$(boot_id)" "$wrapper" "$out" >"$pid_file"
  echo "$out"
}

# Prints the recording's path and ffmpeg's log when it reported anything,
# and exits 1 when ffmpeg ended with an error or logged one: the mp4 may be
# cut short or unplayable, and a silent "ok" would hide that.
finish() {
  local status="$1" out="$2" log="${2%.mp4}.log"
  rm -f "$pid_file" "$status_file"
  echo "$out"
  # 255 is how ffmpeg exits after the SIGINT that ends a recording cleanly.
  if [ "$status" != 0 ] && [ "$status" != 255 ]; then
    echo "yolobox-screen-record: ffmpeg exited with status $status; the recording may be incomplete, log follows:" >&2
    cat "$log" >&2
    exit 1
  fi
  if [ -s "$log" ]; then
    echo "yolobox-screen-record: ffmpeg logged errors; the recording may be incomplete, log follows:" >&2
    cat "$log" >&2
    exit 1
  fi
}

cmd_stop() {
  local status
  mkdir -p "$state_dir"
  take_lock
  if [ ! -f "$pid_file" ]; then
    echo "yolobox-screen-record: not recording (no pid file)" >&2
    exit 1
  fi
  if ! recorder_is_live; then
    # The recording ended on its own: its wrapper left ffmpeg's status.
    if [ -f "$status_file" ]; then
      echo "yolobox-screen-record: the recording had already ended on its own" >&2
      finish "$(cat "$status_file")" "$rec_out"
      exit 1
    fi
    echo "yolobox-screen-record: not recording (stale pid file)" >&2
    exit 1
  fi

  # SIGINT lets ffmpeg finalize the mp4 container; SIGKILL corrupts it. The
  # wrapper, not ffmpeg, is waited for: it exits right after writing the
  # status, and ffmpeg's own exit comes before that. An ffmpeg gone since the
  # check above is no error: the wrapper then reports how it went.
  kill -INT "$rec_pid" 2>/dev/null || true
  # --exited: the wrapper may have exited, and been reaped, before waitpid looks.
  status=0
  waitpid --exited --timeout "$stop_timeout" "$rec_wrapper" || status=$?
  case $status in
    0) ;;
    3)
      echo "yolobox-screen-record: ffmpeg did not exit within ${stop_timeout}s" >&2
      exit 1
      ;;
    *)
      echo "yolobox-screen-record: waitpid failed with status $status while waiting for ffmpeg's wrapper (pid $rec_wrapper)" >&2
      exit 1
      ;;
  esac
  if [ ! -f "$status_file" ]; then
    echo "yolobox-screen-record: ffmpeg's wrapper (pid $rec_wrapper) exited without leaving ffmpeg's status" >&2
    exit 1
  fi
  finish "$(cat "$status_file")" "$rec_out"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  *) usage ;;
esac
