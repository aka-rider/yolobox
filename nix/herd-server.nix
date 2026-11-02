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
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = 5;
      ExecStart = "${lib.getExe herdrPkg} server";
    };
  };
}
