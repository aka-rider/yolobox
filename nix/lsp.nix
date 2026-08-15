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
{ config, ... }:
let
  homeDir = config.users.users.xiii.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;

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

  # pi is absent from this module on purpose. It gains LSP from pi-lens, an
  # npm extension in the user layer (~/.dotfiles/pi/packages.json), which
  # auto-detects basedpyright off the same project devbox PATH and covers
  # every other language too. The flake's own pi-lsp extension registered a
  # second `lsp_diagnostics` tool, and pi refuses to start on a tool-name
  # conflict — one provider only.
  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    dirUser = "xiii";
    dirs = [ ".config" ".config/zed" ".claude" ".claude/skills" ];
    links = [
      { path = ".config/zed/settings.json"; argument = "/etc/yolobox/lsp/zed-settings.json"; }
      { path = ".claude/skills/basedpyright"; argument = "/etc/yolobox/lsp/claude-basedpyright"; }
    ];
  };
}
