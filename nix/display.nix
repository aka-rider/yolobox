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
    runtimeInputs = [ pkgs.ffmpeg-full pkgs.coreutils pkgs.gnused ];
    text = builtins.readFile ./display/yolobox-screen-record.sh;
  };

in
{
  # User units of the agent, not system units running as it: a system
  # unit's log goes to the system journal, which is readable only through
  # the `systemd-journal` group, and that journal also holds the operator's
  # sudo command lines. Same shape as nix/herd-server.nix. DISPLAY=:0 comes
  # from nix/lib/agent-env.nix, the agent's environment, not from the box.
  systemd.user.services.xvfb = {
    description = "Virtual X display :0";
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "default.target" ];
    serviceConfig = {
      Restart = "always";
      # -xkbdir works around nixpkgs#3441 ("cannot find xkbcomp" keymap
      # failure) by pointing Xvfb straight at the store's xkb data.
      # -nolisten tcp keeps the display off the network. There is no -auth,
      # so any local uid can still connect over the unix socket (verified:
      # the operator and root can); that is no boundary, the display is the
      # agent's own.
      ExecStart = "${pkgs.xvfb}/bin/Xvfb :0 -screen 0 1920x1080x24 -nolisten tcp -xkbdir ${pkgs.xkeyboard_config}/etc/X11/xkb";
    };
  };

  systemd.user.services.openbox = {
    description = "openbox on the virtual display";
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "default.target" ];
    after = [ "xvfb.service" ];
    requires = [ "xvfb.service" ];
    environment.DISPLAY = ":0";
    serviceConfig = {
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
    user = agentUser;
    dirs = [ "artifacts" ];
    links = [ ];
  };

  # Playwright's own browser downloads do not run on NixOS, so a Playwright
  # plugin is pointed at nix chromium. PLAYWRIGHT_MCP_BROWSER is deliberately
  # unset: the executable path overrides the channel anyway, and
  # @playwright/mcp's README lists no `chromium` value for it.
  environment.variables = {
    PLAYWRIGHT_MCP_EXECUTABLE_PATH = lib.getExe pkgs.chromium;
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
  };

  fonts.packages = with pkgs; [ dejavu_fonts liberation_ttf noto-fonts noto-fonts-color-emoji ];

  # ffmpeg-full, not the default ffmpeg: nixpkgs' default build is
  # --disable-xlib/--disable-libxcb*, so x11grab doesn't exist in it.
  environment.systemPackages = [ pkgs.chromium pkgs.xdotool pkgs.maim pkgs.ffmpeg-full screenRecord ];
}
