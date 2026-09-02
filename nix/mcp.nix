{ config, lib, pkgs, agentUser, ... }:
let
  cfg = config.yolobox.mcp.servers;
  homeDir = config.users.users.${agentUser}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
  claudeMcpFile = import ./lib/claude-mcp-file.nix;
  basedpyrightLsp = import ./lib/basedpyright.nix;

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
    lsp.basedpyright = {
      command = [ basedpyrightLsp.command ] ++ basedpyrightLsp.args;
      extensions = builtins.attrNames basedpyrightLsp.extensionToLanguage;
    };
  };

  manifest = [
    { path = claudeMcpFile.path; format = "mcpServers"; }
    { path = "/etc/yolobox/mcp/pi.json"; format = "mcpServers"; }
    { path = "/etc/yolobox/mcp/opencode.json"; format = "opencode"; }
  ];

  rendered = builtins.toJSON cfg + builtins.toJSON opencodeConfig;

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
    # A harness that expands command substitution while loading its config
    # would turn a rendered server entry into arbitrary execution. crush did
    # exactly that and is gone, but the guard is kept: it is free at eval time
    # and the next harness added here is not audited in advance.
    assertions = [
      {
        assertion = lib.all (m: !(lib.hasInfix m rendered)) [ "$(" "`" "\${" ];
        message = "yolobox.mcp.servers: a rendered config contains a shell expansion marker ($(, backtick, or \${) — a harness that expands it at config-load time would execute it.";
      }
      {
        assertion = lib.all (s: lib.hasPrefix "/" (toString s.command)) (lib.attrValues cfg);
        message = "yolobox.mcp.servers: every server's command must be an absolute path — no npx.";
      }
    ];

    # claude is deliberately NOT on the enterprise tier
    # (/etc/claude-code/managed-mcp.json): Claude Code refuses every dynamic
    # `--mcp-config` whenever an enterprise MCP config exists, and that is
    # exactly how t3 injects its own t3-code bridge server at turn time — see
    # nix/harnesses.nix. Restoring an enterprise config here would silently
    # break every t3 turn again.
    environment.etc.${claudeMcpFile.etcPath}.text = builtins.toJSON { mcpServers = cfg; };
    environment.etc."yolobox/mcp/pi.json".text = builtins.toJSON { mcpServers = cfg; };
    environment.etc."yolobox/mcp/opencode.json".text = builtins.toJSON opencodeConfig;
    environment.etc."yolobox/mcp/manifest.json".text = builtins.toJSON manifest;

    # Gotcha 12 — nothing reads /etc/yolobox/mcp/*.json unless these are set.
    environment.variables.OPENCODE_CONFIG = "/etc/yolobox/mcp/opencode.json";

    # pi has no native config path for this; it reads ~/.config/mcp/mcp.json
    # via pi-mcp-adapter, and the harness never rewrites it, so a plain
    # symlink (Gotcha 14) tracks /etc across every rebuild.
    systemd.tmpfiles.rules = homeTmpfiles {
      home = homeDir;
      dirUser = agentUser;
      dirs = [ ".config" ".config/mcp" ];
      links = [
        { path = ".config/mcp/mcp.json"; argument = "/etc/yolobox/mcp/pi.json"; }
      ];
    };

    environment.systemPackages = [ mcpSmoke ];
  };
}
