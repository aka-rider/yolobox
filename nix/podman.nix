{ config, lib, pkgs, ... }:
{
  virtualisation.podman = {
    enable = true;
    dockerCompat = true;
    autoPrune.enable = true;
  };

  # Rootless-only: no dockerSocket.enable, no podman group (would grant
  # root-equivalent access and split storage from the rootless CLI).
  systemd.user.sockets.podman.wantedBy = [ "sockets.target" ];

  # $XDG_RUNTIME_DIR must expand at login, not at eval time.
  environment.extraInit = ''
    export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
  '';
}
