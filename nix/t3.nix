{ config, lib, pkgs, username, ... }:
let
  t3 = pkgs.callPackage ./pkgs/t3.nix { };
  homeDir = config.users.users.${username}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
in
{
  systemd.services.t3 = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    # A system service inherits neither a login PATH nor a HOME, and t3 shells
    # out to the harnesses it drives (claude, opencode) and to git, then keeps
    # its state under $HOME/.t3 — the same $HOME an interactive `yo enter`
    # session gets, or `t3 pair` (run interactively to mint a pairing token)
    # can't find this server's runtime file.
    path = [ "/run/current-system/sw" ];
    environment.HOME = homeDir;
    serviceConfig = {
      User = username;
      Group = "users";
      Restart = "always";
      ExecStart = "${lib.getExe t3} serve --host 127.0.0.1 --port 3773";
    };
  };

  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    dirUser = username;
    dirs = [ ".t3" ];
    links = [ ];
  };

  environment.systemPackages = [ t3 ];
}
