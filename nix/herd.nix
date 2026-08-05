{ config, pkgs, ... }:
let
  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = config.yolobox.harness.herdr; };
in
{
  config = {
    # The user manager only starts at boot with linger enabled — without it
    # the herdr server (and any agent panes it hosts) would die the moment
    # the last ssh session to xiii disconnects.
    users.users.xiii.linger = true;

    systemd.user.services.herdr = {
      wantedBy = [ "default.target" ];
      serviceConfig = {
        ExecStart = "${herdrPkg}/bin/herdr server";
        Restart = "on-failure";
      };
    };

    systemd.user.services.yolobox-herdr-integrations = {
      wantedBy = [ "default.target" ];
      after = [ "herdr.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # "-" prefix: an integration install is a best-effort per-harness
        # step, not an all-or-nothing unit — herdr refuses e.g. the opencode
        # integration until opencode itself has been run once and created
        # its config dir, and that must not block installing the others.
        ExecStart = map (kind: "-${herdrPkg}/bin/herdr integration install ${kind}") [
          "claude"
          "pi"
          "opencode"
          "codex"
        ];
      };
    };
  };
}
