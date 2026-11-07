# The guest half of `yo`: one subcommand table over ssh, built as a
# shellchecked package. See nix/guest/yolobox-guest.sh for the subcommands
# themselves.
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
    runtimeInputs = [
      pkgs.coreutils
      pkgs.findutils
      pkgs.gawk
      pkgs.git
      pkgs.curl
      pkgs.dnsutils
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
  # Reachable by name, so CI can build the package on its own: its check
  # phase (shellcheck) runs only in a build (nix/display.md).
  system.build.yoloboxGuestScript = yoloboxGuest;
}
