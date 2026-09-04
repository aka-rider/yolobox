{ config, lib, pkgs, agentUser, ... }:
let
  cfg = config.yolobox.harness;

  claudeHooksFile = import ./lib/claude-hooks-file.nix;
  claudeMcpFile = import ./lib/claude-mcp-file.nix;

  # The herd hook map reaches claude only as `--settings <file>`; see the long
  # note in nix/herd-report.nix for why /etc/claude-code/managed-settings.json
  # is silently voided by a single unrelated key in the account's
  # server-fetched remote settings. A wrapper — rather than an argument added
  # by `yo enter` — is the only chokepoint that also covers sessions nobody
  # types: nix/t3.nix runs `t3 serve`, which spawns claude itself out of
  # /run/current-system/sw.
  #
  # The same reasoning carries the MCP config: `--mcp-config <file>` rides
  # this wrapper rather than any per-session invocation, for the identical
  # reason — t3-spawned claude never goes through `yo enter` or a typed
  # command line, so a wrapper is the only place that reaches it. See
  # nix/mcp.nix for why this file, not the enterprise tier, is where MCP
  # servers are delivered: claude refuses any dynamic `--mcp-config` when an
  # enterprise config is present, and t3 itself passes `--mcp-config` at turn
  # time for its own bridge server, so the enterprise tier and t3 are
  # mutually exclusive.
  #
  # --add-flags puts the flags BEFORE the user's arguments, so a user's own
  # `--settings` or `--mcp-config` later on the command line still wins.
  #
  # The order of the two is load-bearing: `--mcp-config` is variadic
  # (`<configs...>`), so it keeps consuming arguments until the next one
  # starting with `-`. Left last it would eat the user's positional prompt —
  # `claude 'say hi'` dies with "MCP config file not found: .../say hi".
  # Naming `--settings` after it bounds the variadic before user arguments
  # are reached.
  #
  # symlinkJoin + wrapProgram wraps the wrapper: upstream's bin/claude is
  # already a makeCWrapper that sets DISABLE_AUTOUPDATER,
  # FORCE_AUTOUPDATE_PLUGINS, DISABLE_INSTALLATION_CHECKS,
  # USE_BUILTIN_RIPGREP, LD_LIBRARY_PATH and PATH. Wrapping the symlink to it
  # leaves all of that intact and untouched. No meta is inherited on purpose:
  # meta.license would drag the unfree check onto this derivation's own name,
  # which allowUnfreePredicate below does not (and should not) list.
  #
  # The box must therefore be the ONLY claude installation on it, and the
  # reaper below enforces that. A wrapper delivers the hooks by being the
  # `claude` that PATH resolves to, and it cannot win a PATH race against a
  # directory the box does not own: the agent's own dotfiles PREPEND
  # $HOME/.local/bin, and `yo enter` ends in an interactive login zsh, so the
  # moment `claude update` writes a launcher there, every typed `claude` is
  # the bare upstream one — no --settings, no hooks, no herd reporting, and
  # no trace anywhere, because a reporter that never runs logs nothing.
  # A self-updating home install is thus not a nicer delivery channel for the
  # same binary; it is a second installation that silently outranks this one.
  claudeCodePkg = pkgs.symlinkJoin {
    name = "claude-code-herd-hooks-${pkgs.claude-code.version}";
    paths = [ pkgs.claude-code ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      wrapProgram "$out/bin/claude" --add-flags "--mcp-config ${claudeMcpFile.path} --settings ${claudeHooksFile.path}"
    '';
    meta.mainProgram = "claude";
  };

  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = cfg.herdr; };
in
{
  options.yolobox.harness = {
    herdr = {
      # Defaulting to null means the guest tracks nixpkgs' herdr, so the
      # version is no longer a hand-copied duplicate of a host fact that
      # nobody remembers to re-copy.
      #
      # The invariant it has to satisfy: the box's herdr must be the SAME
      # version as the Mac's Homebrew herdr, because herdr's wire protocol
      # changes across patch bumps — 0.8.0 to 0.8.2 went from protocol 19 to
      # 20. On drift the host server answers every report with
      # protocol_mismatch and boxed agents show as `unknown`, with nothing
      # printed anywhere. That is why the drift is now enforced rather than
      # documented: `yo enter` refuses on a version mismatch, and
      # `yo herd-check` proves compatibility end to end.
      #
      # Setting version+hash here re-pins the guest to a GitHub release
      # (nix/pkgs/herdr-bin.nix) — the escape hatch for when nixpkgs lags a
      # herdr release the Mac has already taken.
      version = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "herdr release fetched via nix/pkgs/herdr-bin.nix, bypassing nixpkgs' pin.";
      };
      hash = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "SRI hash of the herdr release binary at yolobox.harness.herdr.version. Setting this activates the override.";
      };
    };
  };

  config = {
    # The ONLY definition site for this predicate (Gotcha 11) — claude-code
    # is nixpkgs' unfree harness.
    nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) [ "claude-code" ];

    # Upstream's own wrapper already sets this for the process it starts; the
    # box-wide variable also reaches a claude started any other way, so no
    # background updater can quietly grow the home install the reaper below
    # exists to remove.
    environment.variables.DISABLE_AUTOUPDATER = "1";

    environment.systemPackages = [
      claudeCodePkg
      pkgs.opencode
      pkgs.pi-coding-agent
      # pi shells out to npm to install the packages listed in its settings.
      pkgs.nodejs
      herdrPkg
    ];

    # A hand-run `claude update` writes ~/.local/bin/claude and, because the
    # agent's dotfiles prepend that directory, from then on shadows the
    # wrapper above in every interactive shell — silently, since the herd
    # reporter that never runs logs nothing. This path unit closes that
    # window as it opens: the launcher's appearance triggers the removal of
    # it and of the versions directory it points into. It cannot loop,
    # because the service deletes exactly the path that armed it and the path
    # unit only rearms if the file comes back. ConditionUser keeps it out of
    # the operator's user manager, which owns no such install.
    #
    # The tmpfiles rules in nix/base.nix cover the same two paths for the
    # case this unit cannot see: an update run while no user manager of the
    # agent's was up. Boot and switch are the only moments those fire, which
    # is exactly why they are a backstop and not the mechanism.
    systemd.user.paths.claude-home-install-reap = {
      description = "Watch for a self-updating claude install shadowing the wrapped one";
      unitConfig.ConditionUser = agentUser;
      wantedBy = [ "paths.target" ];
      pathConfig.PathExists = "%h/.local/bin/claude";
    };
    systemd.user.services.claude-home-install-reap = {
      description = "Remove the self-updating claude install shadowing the wrapped one";
      unitConfig.ConditionUser = agentUser;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = [
          "${pkgs.coreutils}/bin/rm -rf %h/.local/bin/claude %h/.local/share/claude/versions"
          "${pkgs.coreutils}/bin/echo 'removed ~/.local/bin/claude and ~/.local/share/claude/versions: a home claude install shadows the wrapped one on PATH and would run with no herd hooks'"
        ];
      };
    };

    assertions = [
      {
        assertion = (cfg.herdr.version == null) == (cfg.herdr.hash == null);
        message = "yolobox.harness.herdr: version and hash must both be null or both be set.";
      }
    ];
  };
}
