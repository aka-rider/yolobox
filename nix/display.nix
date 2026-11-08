{ config, lib, pkgs, yolobox, ... }:
let
  inherit (yolobox) agentHomeTmpfiles agentUnit display;

  # Type=notify: openbox starts after Xvfb accepts connections.
  xvfb = pkgs.writeShellApplication {
    name = "yolobox-xvfb";
    runtimeInputs = [ pkgs.xvfb config.systemd.package ];
    runtimeEnv = {
      YOLOBOX_XVFB_DISPLAY = display;
      # -xkbdir works around nixpkgs#3441 ("cannot find xkbcomp" keymap
      # failure) by pointing Xvfb straight at the store's xkb data.
      YOLOBOX_XVFB_XKBDIR = "${pkgs.xkeyboard_config}/etc/X11/xkb";
    };
    text = builtins.readFile ./display/yolobox-xvfb.sh;
  };

  screenRecord = pkgs.writeShellApplication {
    name = "yolobox-screen-record";
    # ffmpeg-full, not the default ffmpeg: nixpkgs' default build is
    # --disable-xlib/--disable-libxcb*, so x11grab doesn't exist in it.
    # util-linux for waitpid, which stop waits on ffmpeg with.
    runtimeInputs = [ pkgs.ffmpeg-full pkgs.coreutils pkgs.gnused pkgs.util-linux ];
    text = builtins.readFile ./display/yolobox-screen-record.sh;
  };

in
{
  # User units of the agent, not system units running as it: a system
  # unit's log goes to the system journal, which is readable only through
  # the `systemd-journal` group, and that journal also holds the operator's
  # sudo command lines. DISPLAY is in the agent's environment (agentEnv);
  # openbox, a unit of its own, sets it below.
  systemd.user.services.xvfb = agentUnit {
    description = "Virtual X display ${display}";
    # The display must come back however often it dies: systemd's default
    # five starts in ten seconds would leave the box without a display
    # until the next boot.
    unitConfig.StartLimitIntervalSec = 0;
    wantedBy = [ "default.target" ];
    serviceConfig = {
      Type = "notify";
      # The ready signal comes from a child of the service's main process.
      NotifyAccess = "all";
      Restart = "always";
      # A pause between restarts, so a crash loop does not spin the CPU.
      RestartSec = 2;
      ExecStart = lib.getExe xvfb;
    };
  };

  systemd.user.services.openbox = agentUnit {
    description = "openbox on the virtual display";
    wantedBy = [ "default.target" ];
    after = [ "xvfb.service" ];
    requires = [ "xvfb.service" ];
    environment.DISPLAY = display;
    serviceConfig = {
      Restart = "always";
      ExecStart = "${pkgs.openbox}/bin/openbox";
    };
  };

  systemd.tmpfiles.rules = agentHomeTmpfiles {
    dirs = [ "artifacts" ];
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
  # Reachable by name, so CI can build the package on its own: its check
  # phase (shellcheck) runs only in a build.
  system.build.yoloboxScreenRecord = screenRecord;
  system.build.yoloboxXvfb = xvfb;
}
