{ lib }:
# Shared tmpfiles pattern for placing a file under a user's $HOME without
# home-manager (plan Gotcha 14). "d" rules must own every parent directory
# as the target user first — otherwise tmpfiles auto-creates them
# root-owned and refuses the leaf rule as an "unsafe path transition" from
# a user-owned $HOME into a root-owned subdirectory.
{ home, dirUser, dirGroup ? "users", dirs, rule, path, leafMode ? "-", leafUser ? "-", leafGroup ? "-", argument }:
let
  dirRules = map (d: "d ${home}/${d} 0755 ${dirUser} ${dirGroup} -") dirs;
  leafRule = "${rule} ${home}/${path} ${leafMode} ${leafUser} ${leafGroup} - ${argument}";
in
dirRules ++ [ leafRule ]
