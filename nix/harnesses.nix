{ config, lib, pkgs, ... }:
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
  # The flags are added BEFORE "$@", so a user's own `--settings` or
  # `--mcp-config` later on the command line still wins.
  #
  # The order of the two is load-bearing: `--mcp-config` is variadic
  # (`<configs...>`), so it keeps consuming arguments until the next one
  # starting with `-`. Left last it would eat the user's positional prompt —
  # `claude 'say hi'` dies with "MCP config file not found: .../say hi".
  # Naming `--settings` after it bounds the variadic before user arguments
  # are reached.
  #
  # This execs the stock launcher rather than replacing it: since Claude Code
  # 2.1.207, `claude update` installs the real binary under
  # ~/.local/share/claude/versions and points ~/.local/bin/claude at it, but
  # only when that path was left alone. A custom launcher placed there stops
  # version pruning (every update keeps piling up ~216MB files) and makes
  # `claude doctor` report a launcher it did not create. So this wrapper
  # stays on PATH as a nix store script and execs whichever install is
  # current — the self-updating home copy once `claude update` has run once,
  # the pinned nix store copy on a fresh box that has not run it yet.
  #
  # ~/.local/bin is appended to PATH, never prepended: `claude update` warns
  # on every run when the launcher's directory is not on PATH at all, and
  # DISABLE_INSTALLATION_CHECKS does not silence that particular warning.
  # Appending satisfies the check while this wrapper still shadows the bare
  # launcher. environment.localBinInPath would prepend instead, and the
  # unwrapped launcher would then win in every interactive shell — claude
  # keeps working and silently stops carrying the herd hooks.
  claudeCodePkg = pkgs.writeShellScriptBin "claude" ''
    export PATH="''${PATH}:''${HOME}/.local/bin"
    if [ -x "''${HOME}/.local/bin/claude" ]; then
      exec "''${HOME}/.local/bin/claude" --mcp-config ${claudeMcpFile.path} --settings ${claudeHooksFile.path} "''$@"
    fi
    echo "claude: running the nix store copy ${pkgs.claude-code.version}; run 'claude update' once to switch to the self-updating install under ~/.local/share/claude" >&2
    exec ${pkgs.claude-code}/bin/claude --mcp-config ${claudeMcpFile.path} --settings ${claudeHooksFile.path} "''$@"
  '';

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

    environment.systemPackages = [
      claudeCodePkg
      pkgs.opencode
      pkgs.pi-coding-agent
      # pi shells out to npm to install the packages listed in its settings.
      pkgs.nodejs
      herdrPkg
    ];

    assertions = [
      {
        assertion = (cfg.herdr.version == null) == (cfg.herdr.hash == null);
        message = "yolobox.harness.herdr: version and hash must both be null or both be set.";
      }
    ];
  };
}
