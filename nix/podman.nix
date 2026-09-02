{ config, lib, pkgs, agentUser, ... }:
{
  virtualisation.podman = {
    enable = true;
    dockerCompat = true;
  };

  # Rootless-only: no dockerSocket.enable, no podman group (would grant
  # root-equivalent access and split storage from the rootless CLI).
  systemd.user.sockets.podman.wantedBy = [ "sockets.target" ];

  # autoPrune.enable renders a system-scope timer running as root, which has
  # nothing to prune on a rootless-only box; prune as the account that owns
  # the storage instead.
  systemd.user.services.podman-prune = {
    description = "Prune unused rootless podman storage";
    unitConfig.ConditionUser = agentUser;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${config.virtualisation.podman.package}/bin/podman system prune -f";
    };
  };
  systemd.user.timers.podman-prune = {
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "weekly";
      Persistent = true;
      RandomizedDelaySec = "1h";
    };
  };

  # $XDG_RUNTIME_DIR must expand at login, not at eval time.
  environment.extraInit = ''
    export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
  '';
}
