{ config, lib, pkgs, agentUser, ... }:
let
  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = config.yolobox.harness.herdr; };
  agentEnv = import ./lib/agent-env.nix {
    agentHome = config.users.users.${agentUser}.home;
  };
in
{
  systemd.user.services.herdr-server = {
    description = "herdr server";
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "default.target" ];
    path = [ "/run/current-system/sw" ];
    environment = agentEnv.env;
    # A switch must not stop the server: that kills the panes, and lets the host's remote-client-bridge start a server outside this unit.
    restartIfChanged = false;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = 5;
      # Panes inherit this; base.nix gives user@.service -500, and a runaway build in a pane must not outrank system services.
      OOMScoreAdjust = 0;
      ExecStart = "${lib.getExe herdrPkg} server";
    };
  };
}
