# The guest half of `yo`: one subcommand table over ssh, built as a
# shellchecked package.
{ pkgs, ... }:
let
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
    text = builtins.readFile ./guest/yolobox-guest.sh;
  };
in
{
  # environment.systemPackages, not a user-scoped package: both accounts run
  # subcommands from this helper (gc-machine as the operator, everything
  # else as the agent).
  environment.systemPackages = [ yoloboxGuest ];
  # Reachable by name, so CI can build the package on its own: its check
  # phase (shellcheck) runs only in a build.
  system.build.yoloboxGuestScript = yoloboxGuest;
}
