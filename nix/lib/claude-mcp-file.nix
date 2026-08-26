# Single definition site for the path of claude's MCP config, consumed by
# nix/mcp.nix (which renders the file) and harnesses.nix (which wraps claude
# to pass it with --mcp-config). The two cannot drift.
let
  etcPath = "yolobox/mcp/claude.json";
in
{
  inherit etcPath;
  path = "/etc/${etcPath}";
}
