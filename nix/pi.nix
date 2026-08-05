{ config, lib, pkgs, ... }:
let
  homeDir = config.users.users.xiii.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;

  piMcpAdapter = pkgs.callPackage ./pkgs/pi-mcp-adapter.nix { };

  piSettings = {
    packages = [ "${piMcpAdapter}/lib/node_modules/pi-mcp-adapter" ];
  };
in
{
  config = {
    environment.etc."yolobox/pi/settings.json".text = builtins.toJSON piSettings;

    # pi may rewrite ~/.pi/agent/settings.json (e.g. via /extensions), so
    # this is copy-if-absent (Gotcha 14), not a symlink.
    systemd.tmpfiles.rules = homeTmpfiles {
      home = homeDir;
      dirUser = "xiii";
      dirs = [ ".pi" ".pi/agent" ];
      rule = "C";
      path = ".pi/agent/settings.json";
      leafMode = "0644";
      leafUser = "xiii";
      leafGroup = "users";
      argument = "/etc/yolobox/pi/settings.json";
    };
  };
}
