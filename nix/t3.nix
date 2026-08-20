{ config, lib, pkgs, ... }:
let
  t3 = pkgs.callPackage ./pkgs/t3.nix { };
in
{
  systemd.services.t3 = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    # A system service inherits neither a login PATH nor a HOME, and t3 shells
    # out to the harnesses it drives (claude, opencode) and to git, then keeps
    # its state under $HOME/.t3code.
    path = [ "/run/current-system/sw" ];
    environment.HOME = config.users.users.xiii.home;
    serviceConfig = {
      User = "xiii";
      Group = "users";
      Restart = "always";
      ExecStart = "${lib.getExe t3} serve --host 127.0.0.1 --port 3773";
    };
  };

  environment.systemPackages = [ t3 ];
}
