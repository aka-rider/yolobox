{ config, lib, pkgs, yolobox, facts, ... }:
let
  inherit (yolobox) agentUnit;
in
{
  virtualisation.podman = {
    enable = true;
    dockerCompat = true;
  };

  # A container's stdout and podman's own healthcheck events must not land in
  # the journal: frequent compose healthchecks flood it. k8s-file keeps
  # `podman logs` working, bounded per container and deleted with it; the file
  # events logger keeps `podman events` working without journald. A container
  # keeps the log driver it was created with, so an old one must be recreated.
  virtualisation.containers.containersConf.settings = {
    containers = {
      log_driver = "k8s-file";
      log_size_max = 10 * 1024 * 1024;
    };
    engine.events_logger = "file";
  };

  # Rootless-only: no dockerSocket.enable, no podman group (would grant
  # root-equivalent access and split storage from the rootless CLI). The
  # socket is the agent's: the operator's user manager runs no podman.
  systemd.user.sockets.podman = agentUnit {
    wantedBy = [ "sockets.target" ];
  };

  # autoPrune.enable renders a system-scope timer running as root, which has
  # nothing to prune on a rootless-only box; prune as the account that owns
  # the storage instead. `podman system prune -f` removes stopped containers,
  # unused networks, dangling images and build cache; volumes are kept.
  systemd.user.services.podman-prune = agentUnit {
    description = "Prune stopped containers and unused rootless podman storage";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${config.virtualisation.podman.package}/bin/podman system prune -f";
    };
  };
  systemd.user.timers.podman-prune = agentUnit {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "weekly";
      Persistent = true;
      RandomizedDelaySec = "1h";
    };
  };

  # $XDG_RUNTIME_DIR must expand at login, not at eval time. Only the agent
  # has the socket, so only its shells name it.
  environment.extraInit = ''
    if [ "$(id -un)" = ${lib.escapeShellArg facts.agent_user} ]; then
      export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
    fi
  '';
}
