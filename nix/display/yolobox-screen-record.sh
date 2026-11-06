#!/usr/bin/env bash
# Records the virtual display to an mp4 under ~/artifacts. Built by
# nix/display.nix with writeShellApplication, which prepends
# `set -o errexit -o nounset -o pipefail` and puts the `runtimeInputs` listed
# there on PATH.
# See nix/display.md.

# State lives under ~/.local/state, not /run/user: /run/user is torn
# down at last logout while a backgrounded ffmpeg keeps running,
# orphaning an unfinalized mp4 with no way to stop it cleanly.
state_dir="$HOME/.local/state/yolobox"
pid_file="$state_dir/screen-record.pid"
progress_fifo="$state_dir/screen-record.progress"
# Seconds start waits for ffmpeg's first progress report, and stop waits for
# ffmpeg to finalize the mp4 after SIGINT.
start_timeout=10
stop_timeout=20

usage() {
  echo "usage: yolobox-screen-record {start|stop}" >&2
  exit 1
}

# Prints the pid in the pid file when that process is a running ffmpeg, and
# prints nothing otherwise. The file outlives a reboot, and the kernel hands the
# pid to some other process by then, so a live pid alone proves nothing: it must
# be an ffmpeg. A pid file that does not name one is stale and is removed.
live_recorder_pid() {
  local pid
  [ -f "$pid_file" ] || return 0
  pid="$(sed -n '1p' "$pid_file")"
  case "$pid" in
    '' | *[!0-9]*) ;;
    *)
      if [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = ffmpeg ]; then
        echo "$pid"
        return 0
      fi
      ;;
  esac
  rm -f "$pid_file"
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

cmd_start() {
  local old_pid dir out log pid ready code
  old_pid="$(live_recorder_pid)"
  if [ -n "$old_pid" ]; then
    echo "yolobox-screen-record: already recording (pid $old_pid)" >&2
    exit 1
  fi

  mkdir -p "$state_dir"
  dir="${YOLOBOX_ARTIFACTS:-$HOME/artifacts}"
  mkdir -p "$dir"
  out="$dir/screen-$(date +%Y%m%d-%H%M%S).mp4"
  log="${out%.mp4}.log"

  rm -f "$progress_fifo"
  mkfifo "$progress_fifo"
  ffmpeg -nostdin -loglevel error -progress pipe:3 -f x11grab \
    -video_size 1920x1080 -framerate 15 -i "${DISPLAY:-:0}" -c:v libx264 \
    -preset ultrafast -pix_fmt yuv420p "$out" \
    >/dev/null 2>"$log" 3>"$progress_fifo" &
  pid=$!
  exec 4<"$progress_fifo"
  rm "$progress_fifo"

  ready=0
  await_first_progress || ready=$?
  case $ready in
    1)
      code=0
      wait "$pid" || code=$?
      echo "yolobox-screen-record: ffmpeg exited with status $code before it started recording, log follows:" >&2
      cat "$log" >&2
      exit 1
      ;;
    2)
      # It may have exited since the read gave up; then there is nothing to kill.
      kill -KILL "$pid" 2>/dev/null || true
      echo "yolobox-screen-record: ffmpeg did not start recording within ${start_timeout}s, killed it; log follows:" >&2
      cat "$log" >&2
      exit 1
      ;;
  esac
  # Something must read the pipe until ffmpeg exits: ffmpeg survives writing
  # to a pipe no one reads, but logs it as an error when it finishes.
  cat <&4 >/dev/null 2>&1 &
  exec 4<&-

  printf '%s\n%s\n' "$pid" "$out" >"$pid_file"
  echo "$out"
}

cmd_stop() {
  local pid out status
  if [ ! -f "$pid_file" ]; then
    echo "yolobox-screen-record: not recording (no pid file)" >&2
    exit 1
  fi

  out="$(sed -n '2p' "$pid_file")"
  pid="$(live_recorder_pid)"
  if [ -z "$pid" ]; then
    echo "yolobox-screen-record: not recording (stale pid file)" >&2
    exit 1
  fi

  # SIGINT lets ffmpeg finalize the mp4 container; SIGKILL corrupts it.
  kill -INT "$pid"
  # --exited: ffmpeg may have exited, and been reaped, before waitpid looks.
  status=0
  waitpid --exited --timeout "$stop_timeout" "$pid" || status=$?
  case $status in
    0) ;;
    3)
      echo "yolobox-screen-record: ffmpeg did not exit within ${stop_timeout}s" >&2
      exit 1
      ;;
    *)
      echo "yolobox-screen-record: waitpid failed with status $status while waiting for ffmpeg (pid $pid)" >&2
      exit 1
      ;;
  esac

  rm -f "$pid_file"
  echo "$out"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  *) usage ;;
esac
