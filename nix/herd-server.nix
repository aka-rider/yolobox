{ lib, pkgs, yolobox, facts, ... }:
let
  herdrAgentLease = pkgs.writeShellApplication {
    name = "herdr-agent-lease";
    runtimeInputs = [ pkgs.socat ];
    runtimeEnv = {
      HERDR_API_SOCK = facts.herdr_api_sock;
      AGENT_AUTH_SOCK = facts.agent_auth_sock;
    };
    text = builtins.readFile ./guest/herdr-agent-lease.sh;
  };
in
{
  system.build.yoloboxHerdrAgentLease = herdrAgentLease;

  systemd.user.services.herdr-server = yolobox.agentUnit {
    description = "herdr server";
    wantedBy = [ "default.target" ];
    path = [ "/run/current-system/sw" ];
    environment = yolobox.agentEnv;
    # A switch must not stop the server: that kills the panes, and lets the host's remote-client-bridge start a server outside this unit.
    restartIfChanged = false;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = 5;
      # Panes inherit this; base.nix gives user@.service -500, and a runaway build in a pane must not outrank system services.
      OOMScoreAdjust = 0;
      ExecStart = "${lib.getExe yolobox.herdrPkg} server";
    };
  };

  # herdr fixes the panes' agent fallback when its server starts, which is
  # before yo starts the agent socket, so the box holds its own registration
  # instead of relying on a host client's.
  systemd.user.paths.herdr-agent-lease = yolobox.agentUnit {
    description = "Start the herdr agent lease once the agent socket exists";
    wantedBy = [ "default.target" ];
    pathConfig.PathExists = facts.agent_auth_sock;
  };

  # StartLimitIntervalSec = 0: a herdr server restart ends the lease, and the
  # holder must keep coming back past systemd's default burst limit.
  systemd.user.services.herdr-agent-lease = yolobox.agentUnit {
    description = "Hold the box's SSH agent registration with the herdr server";
    requires = [ "herdr-server.service" ];
    after = [ "herdr-server.service" ];
    partOf = [ "herdr-server.service" ];
    unitConfig.StartLimitIntervalSec = 0;
    serviceConfig = {
      Restart = "always";
      RestartSec = 1;
      ExecStart = lib.getExe herdrAgentLease;
    };
  };
}
