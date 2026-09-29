{ config, lib, pkgs, agentUser, ... }:
let
  homeDir = config.users.users.${agentUser}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
  agentEnv = import ./lib/agent-env.nix { agentHome = homeDir; };
  nixLdEnv = import ./lib/nix-ld-env.nix;

  t3VersionsDir = "${homeDir}/.t3/runtime/versions";
  t3Link = "${homeDir}/.local/bin/t3";

  # Mirrors claudeLauncherKeeper (nix/harnesses.nix): the vendor's own
  # install.sh and `t3 update -y` both leave every downloaded version behind
  # under runtime/versions, ~200 MB each, and nothing else prunes them. A
  # completed version is a plain directory; install.sh only ever stages one
  # under a hidden `.staging-XXXXXX` name and atomically `mv`s it into place
  # already carrying `.install-complete`, so excluding dotdirs is enough to
  # skip an install still in flight — no separate sentinel check needed.
  t3PruneKeeper = pkgs.writeShellApplication {
    name = "yolobox-t3-prune";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    text = ''
      keep_versions=2
      settle_minutes=10

      current_dir="$(dirname "$(readlink -f "${t3Link}" 2>/dev/null || true)" 2>/dev/null || true)"

      stale="$(find "${t3VersionsDir}" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -mmin "+$settle_minutes" -printf '%f\n' 2>/dev/null | sort -V | head -n "-$keep_versions" || true)"
      if [ -n "$stale" ]; then
        while IFS= read -r version; do
          dir="${t3VersionsDir}/$version"
          if [ "$dir" = "$current_dir" ]; then
            continue
          fi
          rm -rf "$dir"
          echo "pruned $dir"
        done <<< "$stale"
      fi
    '';
  };
in
{
  # t3code installs and updates itself from its own vendor script
  # (nix/harnesses.nix), the same way claude does — the box owns only its
  # environment. `t3 service install` writes the real unit,
  # ~/.config/systemd/user/t3code.service, with its own ExecStart,
  # WorkingDirectory and log redirection, and `t3 update -y` rewrites that
  # same file on every update; this is a drop-in on top of it, never a
  # replacement, so it survives every update unaffected. `overrideStrategy
  # = "asDropin"` writes only /etc/systemd/user/t3code.service.d/overrides.conf
  # — with no unit file of ours at that name, systemd finds the vendor's own
  # unit under ~/.config/systemd/user first and merges this drop-in onto it.
  systemd.user.services.t3code = {
    overrideStrategy = "asDropin";
    unitConfig.ConditionUser = agentUser;
    # ~/.local first so a t3-spawned claude goes through the box launcher
    # that sets HERDR_AGENT and the Playwright settings file
    # (nix/harnesses.nix), and so opencode resolves at all.
    path = [ "${homeDir}/.local" "/run/current-system/sw" ];
    environment = agentEnv.env // nixLdEnv // {
      HOME = homeDir;
      T3CODE_HOST = "127.0.0.1";
      T3CODE_PORT = "3773";
    };
  };

  systemd.tmpfiles.rules = homeTmpfiles {
    home = homeDir;
    dirUser = agentUser;
    dirs = [ ".t3" ];
    links = [ ];
  };

  systemd.user.paths.yolobox-t3-prune = {
    description = "Watch t3code's version store";
    unitConfig.ConditionUser = agentUser;
    wantedBy = [ "paths.target" ];
    pathConfig.PathModified = [ t3VersionsDir ];
  };
  systemd.user.services.yolobox-t3-prune = {
    description = "Prune old t3code versions";
    unitConfig.ConditionUser = agentUser;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = lib.getExe t3PruneKeeper;
    };
  };
}
