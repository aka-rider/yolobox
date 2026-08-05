# Shared chokepoint for the herdr package selection, consumed by both
# harnesses.nix (systemPackages, version assertion) and herd-report.nix (the
# guest-side reporting bridge) — keeps the pkgs.herdr vs. herdr-bin.nix
# override valve defined exactly once.
{ pkgs, cfg }:
if cfg.hash != null then
  pkgs.callPackage ../pkgs/herdr-bin.nix {
    inherit (cfg) version hash;
  }
else
  pkgs.herdr
