# The guest half of `yo`: one subcommand table over ssh instead of ~200
# lines of bash carried as string constants in yo's Python source. See
# nix/guest/yolobox-guest.sh for the subcommands themselves.
{ pkgs, config, agentUser, ... }:
let
  agentEnv = import ./lib/agent-env.nix {
    agentHome = config.users.users.${agentUser}.home;
  };
  yoloboxGuest = pkgs.writeShellApplication {
    name = "yolobox-guest";
    # Deliberately no nodejs and no podman: runtimeInputs *prefixes* PATH, so
    # either would shadow the agent's vendor-installed npm and the rootless
    # podman wrapper the user tier must use.
    # jq, nft and tailscale: egress-status reads the tailnet and the kill
    # switch table, and net-check reads the state file. tailscale is the
    # module's own package, so the helper and the daemon are the same build.
    runtimeInputs = [
      pkgs.coreutils
      pkgs.findutils
      pkgs.gawk
      pkgs.git
      pkgs.curl
      pkgs.jq
      pkgs.nftables
      config.services.tailscale.package
    ];
    runtimeEnv = {
      YOLOBOX_AWS_BROKER_SOCK = agentEnv.awsBrokerSock;
    };
    text = builtins.readFile ./guest/yolobox-guest.sh;
  };
in
{
  # environment.systemPackages, not a user-scoped package: both accounts run
  # subcommands from this helper (gc-machine as the operator, everything
  # else as the agent).
  environment.systemPackages = [ yoloboxGuest ];
}
