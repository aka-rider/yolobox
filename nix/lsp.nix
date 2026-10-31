# Language-server wiring for the in-VM editors and harnesses.
#
# The design has one chokepoint: each project's devbox provides its language
# servers (nixpkgs builds — Zed's/opencode's own npm downloads are
# glibc-linked and do not run on NixOS), and every consumer below launches
# its server through `lspWrapper`, which runs it under `devbox run` when the
# project has a devbox.json. There is no direnv: nothing loads a project's
# environment on its own, so the server's launch command carries it.
#
# opencode takes its LSP entries from one box-wide config rather than per
# project, so this module owns /etc/yolobox/lsp/opencode.json and the
# OPENCODE_CONFIG that points at it. nix/lib/basedpyright.nix is the shared
# constant every render here builds from.
{ config, pkgs, agentUser, ... }:
let
  homeDir = config.users.users.${agentUser}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;

  basedpyrightLsp = import ./lib/basedpyright.nix;

  # `lspWrapper <server> <args...>`: devbox supplies the server when the
  # working directory is inside a devbox project, found by walking up from
  # $PWD to the first devbox.json; otherwise the bare name must resolve on
  # PATH. The server keeps the project's working directory, so devbox is
  # pointed at the project with `-c` instead of a `cd`. stdout is the
  # server's protocol stream, so package installation on a cold devbox runs
  # first with its output sent to stderr; a failed install shows there and
  # `devbox run` then fails visibly too.
  lspWrapper = pkgs.writeShellScript "yolobox-lsp" ''
    dir=$PWD
    while [ ! -f "$dir/devbox.json" ]; do
      if [ "$dir" = / ]; then
        exec "$@"
      fi
      dir=$(dirname "$dir")
    done
    devbox install -c "$dir" >&2
    exec devbox run -c "$dir" -q -- "$@"
  '';
  basedpyrightArgv = [ basedpyrightLsp.command ] ++ basedpyrightLsp.args;
in
{
  # Zed runs the server named here instead of looking for a bare binary. A
  # server-side setting: the Mac's Zed settings are never consulted for
  # remote projects.
  environment.etc."yolobox/lsp/zed-settings.json".text = builtins.toJSON {
    # The box has no direnv and CONSTITUTION says nothing auto-loads project
    # shell code; disabling the loader makes that true whatever is on PATH.
    load_direnv = "disabled";
    lsp.basedpyright.binary = {
      path = "${lspWrapper}";
      arguments = basedpyrightArgv;
    };
  };

  # claude-code loads LSP servers only through plugins. A plugin directory
  # under ~/.claude/skills is auto-discovered at session start — no
  # marketplace, no trust prompt (user scope).
  environment.etc."yolobox/lsp/claude-basedpyright/.claude-plugin/plugin.json".text =
    builtins.toJSON {
      name = "basedpyright";
      description = "basedpyright LSP launched through the project's devbox";
      version = "1.0.0";
    };
  environment.etc."yolobox/lsp/claude-basedpyright/.lsp.json".text =
    builtins.toJSON {
      basedpyright = {
        command = "${lspWrapper}";
        args = basedpyrightArgv;
        inherit (basedpyrightLsp) extensionToLanguage;
      };
    };

  environment.etc."yolobox/lsp/opencode.json".text = builtins.toJSON {
    "$schema" = "https://opencode.ai/config.json";
    lsp.basedpyright = {
      command = [ "${lspWrapper}" ] ++ basedpyrightArgv;
      extensions = builtins.attrNames basedpyrightLsp.extensionToLanguage;
    };
  };
  environment.variables.OPENCODE_CONFIG = "/etc/yolobox/lsp/opencode.json";

  # pi is absent from this module on purpose. It gains LSP from pi-lens, an
  # npm extension in the user layer (pi's own package list), which
  # auto-detects basedpyright on PATH and covers every other language too.
  # It does not go through `lspWrapper` yet (see TODO.md). The flake's own
  # pi-lsp extension registered a second `lsp_diagnostics` tool, and pi
  # refuses to start on a tool-name conflict: one provider only.
  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    user = agentUser;
    dirs = [ ".config" ".config/zed" ".claude" ".claude/skills" ];
    links = [
      { path = ".config/zed/settings.json"; argument = "/etc/yolobox/lsp/zed-settings.json"; }
      { path = ".claude/skills/basedpyright"; argument = "/etc/yolobox/lsp/claude-basedpyright"; }
    ];
  };
}
