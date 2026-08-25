{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.harness;

  claudeHooksFile = import ./lib/claude-hooks-file.nix;

  claudeCodeUnwrapped =
    if cfg.claude-code.hash != null then
      pkgs.claude-code.overrideAttrs (_: rec {
        version = cfg.claude-code.version;
        # Mirrors the fetchurl pattern in nixpkgs' own claude-code
        # derivation (pkgs/by-name/cl/claude-code/package.nix): binaries are
        # served per-platform from downloads.claude.ai, keyed by node's
        # platform-arch pair, not by nixpkgs' own triple naming.
        src = pkgs.fetchurl {
          url = "https://downloads.claude.ai/claude-code-releases/${version}/${pkgs.stdenv.hostPlatform.node.platform}-${pkgs.stdenv.hostPlatform.node.arch}/claude";
          hash = cfg.claude-code.hash;
        };
      })
    else
      pkgs.claude-code;

  # The herd hook map reaches claude only as `--settings <file>`; see the long
  # note in nix/herd-report.nix for why /etc/claude-code/managed-settings.json
  # is silently voided by a single unrelated key in the account's
  # server-fetched remote settings. A wrapper — rather than an argument added
  # by `yo enter` — is the only chokepoint that also covers sessions nobody
  # types: nix/t3.nix runs `t3 serve`, which spawns claude itself out of
  # /run/current-system/sw.
  #
  # --add-flags puts the flag BEFORE the user's arguments, so a user's own
  # `--settings` later on the command line still wins.
  #
  # symlinkJoin + wrapProgram wraps the wrapper: upstream's bin/claude is
  # already a makeCWrapper that sets DISABLE_AUTOUPDATER,
  # FORCE_AUTOUPDATE_PLUGINS, DISABLE_INSTALLATION_CHECKS,
  # USE_BUILTIN_RIPGREP, LD_LIBRARY_PATH and PATH. Wrapping the symlink to it
  # leaves all of that intact and untouched. No meta is inherited on purpose:
  # meta.license would drag the unfree check onto this derivation's own name,
  # which allowUnfreePredicate below does not (and should not) list.
  claudeCodePkg = pkgs.symlinkJoin {
    name = "claude-code-herd-hooks-${claudeCodeUnwrapped.version}";
    paths = [ claudeCodeUnwrapped ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      wrapProgram "$out/bin/claude" --add-flags "--settings ${claudeHooksFile.path}"
    '';
    meta.mainProgram = "claude";
  };

  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = cfg.herdr; };
in
{
  options.yolobox.harness = {
    claude-code = {
      version = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Override claude-code's release version, bypassing nixpkgs' pin.";
      };
      hash = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "SRI hash of the overridden claude-code release binary. Setting this activates the override.";
      };
    };

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
    # and crush are nixpkgs' unfree harnesses.
    nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) [ "claude-code" "crush" ];

    environment.variables.DISABLE_AUTOUPDATER = "1";

    environment.systemPackages = [
      claudeCodePkg
      pkgs.opencode
      pkgs.crush
      pkgs.pi-coding-agent
      # pi shells out to npm to install the packages listed in its settings.
      pkgs.nodejs
      herdrPkg
    ];

    assertions = [
      {
        assertion = (cfg.claude-code.version == null) == (cfg.claude-code.hash == null);
        message = "yolobox.harness.claude-code: version and hash must both be null or both be set.";
      }
      {
        assertion = (cfg.herdr.version == null) == (cfg.herdr.hash == null);
        message = "yolobox.harness.herdr: version and hash must both be null or both be set.";
      }
    ];
  };
}
