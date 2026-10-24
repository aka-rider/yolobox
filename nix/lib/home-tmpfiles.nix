# Shared tmpfiles pattern for linking files into a user's $HOME without
# home-manager (plan Gotcha 14). "d" rules must own every parent directory
# as the target user first — otherwise tmpfiles auto-creates them
# root-owned and refuses the leaf rule as an "unsafe path transition" from
# a user-owned $HOME into a root-owned subdirectory.
#
# The rules also apply on `switch`, with no reboot: systemd-tmpfiles-resetup
# is the switch-time twin of the boot-only setup unit and re-runs every rule,
# force-replacing "L+" links. nix/harnesses.nix relies on that to hold
# ~/.local/bin/claude, opencode and pi at the box's own paths across the one
# window the launcher path unit cannot see on its own, an install made while
# no user manager of the agent's was running.
{ home, dirUser, dirGroup ? "users", dirs, links }:
let
  dirRules = map (d: "d ${home}/${d} 0755 ${dirUser} ${dirGroup} -") dirs;
  linkRules = map (l: "L+ ${home}/${l.path} - - - - ${l.argument}") links;
in
dirRules ++ linkRules
