{ config, lib, pkgs, agentUser, ... }:
let
  homeDir = config.users.users.${agentUser}.home;
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
    # ffmpeg-full, not the default ffmpeg: nixpkgs' default build is
    # --disable-xlib/--disable-libxcb*, so x11grab doesn't exist in it.
    runtimeInputs = [ pkgs.ffmpeg-full pkgs.coreutils ];
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
          -pix_fmt yuv420p "$out" >/dev/null 2>"$log" &
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
      User = agentUser;
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
      User = agentUser;
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
    dirUser = agentUser;
    dirs = [ "artifacts" ];
    links = [ ];
  };

  # Playwright's own browser downloads do not run on NixOS, so the plugin is
  # pointed at nix chromium. PLAYWRIGHT_MCP_BROWSER is deliberately unset: the
  # executable path overrides the channel anyway, and @playwright/mcp's README
  # lists no `chromium` value for it. agent-browser finds the same chromium on
  # PATH; AGENT_BROWSER_EXECUTABLE_PATH is deliberately NOT set, because
  # pi-agent-browser-native disables its managed session restore whenever that
  # variable is present. Isolated mode and a user-data-dir are mutually
  # exclusive — the server throws — so PLAYWRIGHT_MCP_USER_DATA_DIR must never
  # be set alongside PLAYWRIGHT_MCP_ISOLATED.
  environment.variables = {
    DISPLAY = ":0";
    PLAYWRIGHT_MCP_EXECUTABLE_PATH = lib.getExe pkgs.chromium;
    PLAYWRIGHT_MCP_ISOLATED = "1";
    PLAYWRIGHT_MCP_OUTPUT_DIR = "$HOME/artifacts";
    PLAYWRIGHT_MCP_CAPS = "vision,pdf";
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
  };

  fonts.packages = with pkgs; [ dejavu_fonts liberation_ttf noto-fonts noto-fonts-color-emoji ];

  # ffmpeg-full, not the default ffmpeg: nixpkgs' default build is
  # --disable-xlib/--disable-libxcb*, so x11grab doesn't exist in it.
  environment.systemPackages = [ pkgs.chromium pkgs.xdotool pkgs.maim pkgs.ffmpeg-full screenRecord ];
}
