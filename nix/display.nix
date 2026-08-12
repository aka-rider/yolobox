{ config, lib, pkgs, ... }:
let
  homeDir = config.users.users.xiii.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;

  waitForX = pkgs.writeShellScript "wait-for-x" ''
    for _ in $(seq 1 150); do
      [ -S /tmp/.X11-unix/X0 ] && exit 0
      sleep 0.2
    done
    echo "yolobox: X socket /tmp/.X11-unix/X0 did not appear within 30s" >&2
    exit 1
  '';

  screenRecord = pkgs.writeShellApplication {
    name = "yolobox-screen-record";
    runtimeInputs = [ pkgs.ffmpeg pkgs.coreutils ];
    text = ''
      # State lives under ~/.local/state, not /run/user: /run/user is torn
      # down at last logout while a backgrounded ffmpeg keeps running,
      # orphaning an unfinalized mp4 with no way to stop it cleanly.
      state_dir="$HOME/.local/state/yolobox"
      pid_file="$state_dir/screen-record.pid"

      usage() {
        echo "usage: yolobox-screen-record {start|stop}" >&2
        exit 1
      }

      cmd_start() {
        if [ -f "$pid_file" ]; then
          old_pid="$(sed -n '1p' "$pid_file")"
          if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "yolobox-screen-record: already recording (pid $old_pid)" >&2
            exit 1
          fi
        fi

        mkdir -p "$state_dir"
        dir="''${YOLOBOX_ARTIFACTS:-$HOME/artifacts}"
        mkdir -p "$dir"
        out="$dir/screen-$(date +%Y%m%d-%H%M%S).mp4"
        log="''${out%.mp4}.log"

        ffmpeg -nostdin -loglevel error -f x11grab -video_size 1920x1080 \
          -framerate 15 -i "''${DISPLAY:-:0}" -c:v libx264 -preset ultrafast \
          -pix_fmt yuv420p "$out" 2>"$log" &
        pid=$!

        sleep 0.2
        if ! kill -0 "$pid" 2>/dev/null; then
          echo "yolobox-screen-record: ffmpeg exited immediately, log follows:" >&2
          cat "$log" >&2
          exit 1
        fi

        printf '%s\n%s\n' "$pid" "$out" > "$pid_file"
        echo "$out"
      }

      cmd_stop() {
        if [ ! -f "$pid_file" ]; then
          echo "yolobox-screen-record: not recording (no pid file)" >&2
          exit 1
        fi

        pid="$(sed -n '1p' "$pid_file")"
        out="$(sed -n '2p' "$pid_file")"
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
          rm -f "$pid_file"
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

      case "''${1:-}" in
        start) cmd_start ;;
        stop) cmd_stop ;;
        *) usage ;;
      esac
    '';
  };
in
{
  systemd.services.xvfb = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      User = "xiii";
      Group = "users";
      Restart = "always";
      # -xkbdir works around nixpkgs#3441 ("cannot find xkbcomp" keymap
      # failure) by pointing Xvfb straight at the store's xkb data.
      ExecStart = "${pkgs.xvfb}/bin/Xvfb :0 -screen 0 1920x1080x24 -nolisten tcp -xkbdir ${pkgs.xkeyboard_config}/etc/X11/xkb";
    };
  };

  systemd.services.openbox = {
    wantedBy = [ "multi-user.target" ];
    after = [ "xvfb.service" ];
    requires = [ "xvfb.service" ];
    environment.DISPLAY = ":0";
    serviceConfig = {
      User = "xiii";
      Group = "users";
      Restart = "always";
      # Xvfb's socket lags its own service start; without this wait,
      # openbox crash-loops fast enough to trip systemd's start-limit
      # (5 restarts/10s) and fail the unit permanently instead of settling
      # once Xvfb catches up.
      ExecStartPre = "${waitForX}";
      ExecStart = "${pkgs.openbox}/bin/openbox";
    };
  };

  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    dirUser = "xiii";
    dirs = [ "artifacts" ];
    links = [ ];
  };

  environment.variables.DISPLAY = ":0";

  fonts.packages = with pkgs; [ dejavu_fonts liberation_ttf noto-fonts noto-fonts-color-emoji ];

  environment.systemPackages = [ pkgs.xdotool pkgs.maim pkgs.ffmpeg screenRecord ];
}
