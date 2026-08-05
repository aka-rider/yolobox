{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.mcp.servers;

  serverType = lib.types.submodule {
    options = {
      command = lib.mkOption {
        type = with lib.types; either path str;
        description = "Absolute store path to the MCP server's executable. Never npx — every server is installed at build time and pinned.";
      };
      args = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
      };
      env = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
      };
    };
  };

  # Canonical Claude/pi shape — identity mapping of the option set.
  mcpServers = lib.mapAttrs (_: s: { inherit (s) command args env; }) cfg;

  # crush: near 1:1 with the canonical shape, under "mcp" plus its own
  # top-level $schema.
  crushConfig = {
    "$schema" = "https://charm.land/crush.json";
    mcp = mcpServers;
  };

  # opencode: argv-merged (command + args into one array) and env renamed to
  # "environment" (plan Gotcha 13 / critic R6).
  opencodeConfig = {
    "$schema" = "https://opencode.ai/config.json";
    mcp = lib.mapAttrs
      (_: s: {
        type = "local";
        command = [ s.command ] ++ s.args;
        environment = s.env;
        enabled = true;
      })
      cfg;
  };

  manifest = [
    { path = "/etc/claude-code/managed-mcp.json"; format = "mcpServers"; }
    { path = "/etc/yolobox/mcp/pi.json"; format = "mcpServers"; }
    { path = "/etc/yolobox/mcp/crush.json"; format = "crush"; }
    { path = "/etc/yolobox/mcp/opencode.json"; format = "opencode"; }
  ];

  rendered = builtins.toJSON mcpServers + builtins.toJSON crushConfig + builtins.toJSON opencodeConfig;

  mcpSmoke = pkgs.writeShellApplication {
    name = "yolobox-mcp-smoke";
    runtimeInputs = [ pkgs.jq pkgs.coreutils ];
    text = builtins.readFile ./checks/mcp-smoke.sh;
  };
in
{
  options.yolobox.mcp.servers = lib.mkOption {
    type = lib.types.attrsOf serverType;
    default = { };
    description = "MCP servers, rendered per-harness into every supported config format.";
  };

  config = {
    # crush executes command substitution in its config file at load time
    # (plan Gotcha 13 / mcp/README.md's "Two hard rules") — this is the
    # eval-time enforcement of what v1 stated only as prose.
    assertions = [
      {
        assertion = !(lib.hasInfix "$(" rendered);
        message = "yolobox.mcp.servers: a rendered config contains '$(' — crush executes command substitution in its config at load time.";
      }
    ];

    environment.etc."claude-code/managed-mcp.json".text = builtins.toJSON { mcpServers = mcpServers; };
    environment.etc."yolobox/mcp/pi.json".text = builtins.toJSON { mcpServers = mcpServers; };
    environment.etc."yolobox/mcp/crush.json".text = builtins.toJSON crushConfig;
    environment.etc."yolobox/mcp/opencode.json".text = builtins.toJSON opencodeConfig;
    environment.etc."yolobox/mcp/manifest.json".text = builtins.toJSON manifest;

    # Gotcha 12 — nothing reads /etc/yolobox/mcp/*.json unless these are set.
    environment.variables.OPENCODE_CONFIG = "/etc/yolobox/mcp/opencode.json";
    environment.variables.CRUSH_GLOBAL_CONFIG = "/etc/yolobox/mcp/crush.json";

    # pi has no native config path for this; it reads ~/.config/mcp/mcp.json
    # via pi-mcp-adapter, and the harness never rewrites it, so a plain
    # symlink (Gotcha 14) tracks /etc across every rebuild.
    systemd.tmpfiles.rules = [
      "L+ /home/xiii.guest/.config/mcp/mcp.json - - - - /etc/yolobox/mcp/pi.json"
    ];

    environment.systemPackages = [ mcpSmoke ];
  };
}
