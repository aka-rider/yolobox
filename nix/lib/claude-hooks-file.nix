# Single definition site for the path of claude's settings file, consumed by
# display.nix (which renders it, currently just the playwright artifact
# reroute hook) and harnesses.nix (which wraps claude to pass it with
# --settings). The two cannot drift.
let
  etcPath = "claude-code/claude-hooks.json";
in
{
  inherit etcPath;
  path = "/etc/${etcPath}";
}
