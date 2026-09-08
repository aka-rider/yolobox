# The guest half of `yo`: one subcommand table over ssh instead of ~200
# lines of bash carried as string constants in yo's Python source. See
# nix/guest/yolobox-guest.sh for the subcommands themselves.
{ pkgs, ... }:
let
  yoloboxGuest = pkgs.writeShellApplication {
    name = "yolobox-guest";
    # Deliberately no nodejs and no podman: runtimeInputs *prefixes* PATH, so
    # either would shadow the agent's vendor-installed npm and the rootless
    # podman wrapper the user tier must use.
    runtimeInputs = [ pkgs.coreutils pkgs.findutils pkgs.gawk pkgs.git pkgs.curl ];
    text = builtins.readFile ./guest/yolobox-guest.sh;
  };
in
{
  # environment.systemPackages, not a user-scoped package: both accounts run
  # subcommands from this helper (gc-machine as the operator, everything
  # else as the agent), the same pattern nix/herd-report.nix uses for
  # yolobox-herd-check.
  environment.systemPackages = [ yoloboxGuest ];
}
