{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.harness;

  claudeCodePkg =
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

  herdrPkg =
    if cfg.herdr.hash != null then
      pkgs.callPackage ./pkgs/herdr-bin.nix {
        inherit (cfg.herdr) version hash;
      }
    else
      pkgs.herdr;
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
      version = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "0.8.0";
        description = "herdr release fetched via nix/pkgs/herdr-bin.nix, bypassing nixpkgs' (older) pin.";
      };
      hash = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "sha256-9kesZkaNnvvGQv5TT7KERo8K6mBkFgb8AI38DYKjyoc=";
        description = "SRI hash of the herdr release binary at yolobox.harness.herdr.version. Setting this activates the override.";
      };
    };
  };

  config = {
    # The ONLY definition site for this predicate (Gotcha 11) — claude-code
    # is nixpkgs' sole unfree harness.
    nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) [ "claude-code" ];

    environment.variables.DISABLE_AUTOUPDATER = "1";

    environment.systemPackages = [
      claudeCodePkg
      pkgs.opencode
      pkgs.crush
      pkgs.pi-coding-agent
      herdrPkg
    ];
  };
}
