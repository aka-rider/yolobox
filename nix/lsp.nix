# Language-server wiring for the in-VM editors and harnesses.
#
# The design has one chokepoint: each project's devbox provides its language
# servers (nixpkgs builds — Zed's/opencode's own npm downloads are
# glibc-linked and do not run on NixOS), and direnv puts them on the
# project PATH. Every consumer below therefore names bare binaries, never
# store paths: resolution happens in the per-project environment, unlike
# MCP servers (nix/mcp.nix), which are system-wide and must be absolute.
#
# opencode and crush read their LSP entries from per-project config files
# (opencode.json / .crush.json, committed to each project) because their
# global config files live in nix/mcp.nix, which carries unrelated parallel
# work at the time of writing — lifting the LSP sections into those renders
# is tracked in TODO.md.
{ config, pkgs, ... }:
let
  homeDir = config.users.users.xiii.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;

  piLsp = pkgs.callPackage ./pkgs/pi-lsp.nix { };

  basedpyrightLsp = {
    command = "basedpyright-langserver";
    args = [ "--stdio" ];
    extensionToLanguage = {
      ".py" = "python";
      ".pyi" = "python";
    };
  };
in
{
  # Zed spawns a language server from the project environment only when it
  # loads direnv itself; "direct" makes the remote server run
  # `direnv export json` instead of hoping the shell hook fired. This is a
  # server-side setting: the Mac's Zed settings are never consulted for
  # remote projects.
  environment.etc."yolobox/lsp/zed-settings.json".text = builtins.toJSON {
    load_direnv = "direct";
  };

  # claude-code loads LSP servers only through plugins. A plugin directory
  # under ~/.claude/skills is auto-discovered at session start — no
  # marketplace, no trust prompt (user scope).
  environment.etc."yolobox/lsp/claude-basedpyright/.claude-plugin/plugin.json".text =
    builtins.toJSON {
      name = "basedpyright";
      description = "basedpyright LSP from the project's devbox PATH";
      version = "1.0.0";
    };
  environment.etc."yolobox/lsp/claude-basedpyright/.lsp.json".text =
    builtins.toJSON { basedpyright = basedpyrightLsp; };

  # pi gains LSP through the pi-lsp extension (nix/pkgs/pi-lsp.nix),
  # auto-discovered from ~/.pi/agent/extensions like pi-mcp-adapter in
  # nix/herd-report.nix. Its global config (~/.pi/agent/lsp.json) is
  # auto-trusted, unlike a project-local .pi/lsp.json. Schema note: the
  # command key is "bin", and the file shape is {version, servers[]}.
  environment.etc."yolobox/pi/pi-lsp".source = "${piLsp}/lib/node_modules/pi-lsp";
  environment.etc."yolobox/lsp/pi-lsp.json".text = builtins.toJSON {
    version = 1;
    servers = [
      {
        id = "basedpyright";
        bin = basedpyrightLsp.command;
        args = basedpyrightLsp.args;
        rootMarkers = [ "pyrightconfig.json" "requirements.txt" "pyproject.toml" ];
        languageIdByExtension = basedpyrightLsp.extensionToLanguage;
      }
    ];
  };

  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    dirUser = "xiii";
    dirs = [ ".config" ".config/zed" ".claude" ".claude/skills" ".pi" ".pi/agent" ".pi/agent/extensions" ];
    links = [
      { path = ".config/zed/settings.json"; argument = "/etc/yolobox/lsp/zed-settings.json"; }
      { path = ".claude/skills/basedpyright"; argument = "/etc/yolobox/lsp/claude-basedpyright"; }
      { path = ".pi/agent/extensions/pi-lsp"; argument = "/etc/yolobox/pi/pi-lsp"; }
      { path = ".pi/agent/lsp.json"; argument = "/etc/yolobox/lsp/pi-lsp.json"; }
    ];
  };
}
