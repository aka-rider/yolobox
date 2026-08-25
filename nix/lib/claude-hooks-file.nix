# Single definition site for the path of claude's herd hook map, consumed by
# herd-report.nix (which renders the file) and harnesses.nix (which wraps
# claude to pass it with --settings). The two cannot drift.
let
  etcPath = "claude-code/herd-hooks.json";
in
{
  inherit etcPath;
  path = "/etc/${etcPath}";
}
