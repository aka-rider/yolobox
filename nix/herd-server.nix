{ lib, yolobox, ... }:
{
  systemd.user.services.herdr-server = yolobox.agentUnit {
    description = "herdr server";
    wantedBy = [ "default.target" ];
    path = [ "/run/current-system/sw" ];
    environment = yolobox.agentEnv;
    # A switch must not stop the server: that kills the panes, and lets the host's remote-client-bridge start a server outside this unit.
    restartIfChanged = false;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = 5;
      # Panes inherit this; base.nix gives user@.service -500, and a runaway build in a pane must not outrank system services.
      OOMScoreAdjust = 0;
      ExecStart = "${lib.getExe yolobox.herdrPkg} server";
    };
  };
}
