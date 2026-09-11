{ config, lib, pkgs, agentUser, ... }:
let
  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = config.yolobox.harness.herdr; };
in
{
  systemd.user.services.herdr-server = {
    description = "herdr server";
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "default.target" ];
    path = [ "/run/current-system/sw" ];
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = 5;
      ExecStart = "${lib.getExe herdrPkg} server";
    };
  };
}
