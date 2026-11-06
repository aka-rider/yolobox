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

cmd_start() {
  local old_pid dir out log pid
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

  ffmpeg -nostdin -loglevel error -f x11grab -video_size 1920x1080 \
    -framerate 15 -i "${DISPLAY:-:0}" -c:v libx264 -preset ultrafast \
    -pix_fmt yuv420p "$out" >/dev/null 2>"$log" &
  pid=$!

  sleep 0.2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "yolobox-screen-record: ffmpeg exited immediately, log follows:" >&2
    cat "$log" >&2
    exit 1
  fi

  printf '%s\n%s\n' "$pid" "$out" >"$pid_file"
  echo "$out"
}

cmd_stop() {
  local pid out
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
  for _ in $(seq 1 100); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "yolobox-screen-record: ffmpeg did not exit within 20s" >&2
    exit 1
  fi

  rm -f "$pid_file"
  echo "$out"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  *) usage ;;
esac
