# The guest half of `yo`: one subcommand table over ssh instead of ~200
# lines of bash carried as string constants in yo's Python source. See
# nix/guest/yolobox-guest.sh for the subcommands themselves.
{ lib, pkgs, config, agentUser, ... }:
let
  agentEnv = import ./lib/agent-env.nix {
    agentHome = config.users.users.${agentUser}.home;
  };
  yoloboxGuest = pkgs.writeShellApplication {
    name = "yolobox-guest";
    # Deliberately no nodejs and no podman: runtimeInputs *prefixes* PATH, so
    # either would shadow the agent's vendor-installed npm and the rootless
    # podman wrapper the user tier must use. fzf backs `pick-project`, the
    # guest half of `yo enter [fuzzy]` below.
    runtimeInputs = [ pkgs.coreutils pkgs.findutils pkgs.gawk pkgs.git pkgs.curl pkgs.fzf ];
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

  # `yo enter [fuzzy]`, for use inside a yolobox herdr pane: strictly
  # in-guest, no cross-machine reach back to the Mac's own `yo`. cd has to
  # land in the caller's own shell, which only a shell function can do — a
  # script `exec`s in a child process and its cd dies with it — so this is
  # defined here rather than shipped as another binary next to
  # yolobox-guest. Agent-only: the operator's own shell keeps `yo` meaning
  # "run this on the Mac" (yo up, yo ssh), so it must not be shadowed there.
  environment.interactiveShellInit = ''
    if [ "$(id -un)" = ${lib.escapeShellArg agentUser} ]; then
      yo() {
        if [ "$#" -eq 0 ] || [ "$1" != enter ]; then
          printf 'yo: `yo <cmd>` runs on the Mac; inside yolobox only `yo enter [fuzzy]` exists\n' >&2
          return 2
        fi
        if [ "$#" -eq 1 ]; then
          cd "$HOME"
          return
        fi
        if [ "$#" -gt 2 ]; then
          printf 'yo: enter takes at most one query argument\n' >&2
          return 2
        fi
        local dir
        dir=$(yolobox-guest pick-project "$2") && cd "$dir"
      }
    fi
  '';
}
