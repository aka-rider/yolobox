{ config, lib, pkgs, agentUser, ... }:
let
  cfg = config.yolobox.harness;

  homeDir = config.users.users.${agentUser}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
  nixLdEnv = import ./lib/nix-ld-env.nix;

  launcherPath = "/etc/yolobox/bin/claude";
  launcherLink = "${homeDir}/.local/bin/claude";
  claudeVersionsDir = "${homeDir}/.local/share/claude/versions";

  # Exists while the system's network is up. A user unit cannot order itself
  # after a system unit, so the system side drops this flag and a user path
  # unit waits for it (nix/harnesses.md#install-phases).
  networkReadyFlag = "/run/yolobox-network-ready";

  # Claude Code >= 2.1.207 documents that a custom ~/.local/bin/claude is left
  # alone: `claude update` and the background updater only drop new binaries
  # into ~/.local/share/claude/versions/. The box therefore OWNS that path with
  # a launcher of its own rather than putting a wrapper on the system PATH and
  # racing it: the agent's shell rc files may prepend $HOME/.local/bin, so any
  # wrapper behind it loses in every interactive shell, silently. The launcher
  # exists to export HERDR_AGENT (see below).
  #
  # No runtimeInputs: that would prepend store paths to PATH, and this script
  # execs claude, whose children (the project's own tools) must keep the PATH
  # the project's shell gave them. The few external tools are named by store
  # path instead.
  claudeLauncher = pkgs.writeShellApplication {
    name = "claude";
    runtimeInputs = [ ];
    text = ''
      versions="${claudeVersionsDir}"

      # Newest executable under $versions. Extra find tests go in "$@". No
      # versions directory is no install; any other find error is shown.
      newest_version() {
        [ -d "$versions" ] || return 0
        ${pkgs.findutils}/bin/find "$versions" -maxdepth 1 -type f -perm -u+x "$@" -printf '%f\n' \
          | ${pkgs.coreutils}/bin/sort -V | ${pkgs.coreutils}/bin/tail -n 1 || true
      }

      # A binary modified in the last minute may still be downloading; skip it
      # unless it is all there is (a first install would otherwise have no
      # claude for a minute).
      newest="$(newest_version ! -mmin -1)"
      if [ -z "$newest" ]; then
        newest="$(newest_version)"
      fi

      if [ -z "$newest" ]; then
        echo "claude: no installed version under $versions" >&2
        echo "  the vendor install runs as a user unit: systemctl --user status yolobox-harness-install" >&2
        echo "  its output: journalctl --user -u yolobox-harness-install" >&2
        exit 127
      fi

      # herdr's classification keys off the foreground process name, but the
      # exec'd binary here is named after its version directory (e.g.
      # "2.1.268"), not "claude" - so herdr never recognises it without help.
      export HERDR_AGENT=claude
      exec "$versions/$newest" "$@"
    '';
  };

  claudeLauncherKeeper = pkgs.writeShellApplication {
    name = "yolobox-claude-launcher-keep";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    text = ''
      keep_versions=2
      settle_minutes=10

      if [ "$(readlink "${launcherLink}" || true)" != "${launcherPath}" ]; then
        mkdir -p "$(dirname "${launcherLink}")"
        ln -sfn "${launcherPath}" "${launcherLink}.tmp"
        mv -T "${launcherLink}.tmp" "${launcherLink}"
        echo "relinked ${launcherLink} -> ${launcherPath}"
      fi

      # Rank every version first, so a fresh download counts toward the
      # $keep_versions kept (the launcher runs the newest, which ranking
      # always spares). Only then does settling filter the rest: one the
      # updater finished with more than $settle_minutes minutes ago, so a
      # download in flight is never a pruning target. Nothing else prunes
      # them: keeping every version is precisely what the custom launcher
      # buys, at ~330 MB each.
      older=""
      if [ -d "${claudeVersionsDir}" ]; then
        older="$(find "${claudeVersionsDir}" -maxdepth 1 -type f -perm -u+x -printf '%f\n' | sort -V | head -n "-$keep_versions" || true)"
      fi
      if [ -n "$older" ]; then
        while IFS= read -r version; do
          if [ -n "$(find "${claudeVersionsDir}/$version" -maxdepth 0 -mmin "+$settle_minutes" -print)" ]; then
            rm -f "${claudeVersionsDir}/$version"
            echo "pruned ${claudeVersionsDir}/$version"
          fi
        done <<< "$older"
      fi
    '';
  };

  harnessInstall = pkgs.writeShellApplication {
    name = "yolobox-harness-install";
    text = ''
      cd "$HOME"
      mkdir -p "$HOME/.local/bin"

      # Atomic: a reader never sees the link missing.
      link_bin() {
        ln -sfn "$2" "${homeDir}/.local/bin/$1.tmp"
        mv -T "${homeDir}/.local/bin/$1.tmp" "${homeDir}/.local/bin/$1"
      }

      # Each vendor is an independent phase: one vendor's outage (a down
      # download host, a bad config file) must not keep the others from
      # installing. A phase runs in its own errexit subshell, so its first
      # failing command ends that phase and nothing else; the failure is
      # recorded, and the unit still exits non-zero at the very end so
      # Restart=on-failure retries. Every phase is idempotent, so a retry
      # redoes only what is missing.
      failed=()
      phase() {
        local name="$1" status=0
        shift
        set +e
        ( set -e; "$@" )
        status=$?
        set -e
        if [ "$status" -ne 0 ]; then
          echo "yolobox-harness-install: phase '$name' failed (exit $status)" >&2
          failed+=("$name")
        fi
      }

      # grep -q exits at its first match, and a producer still writing then
      # dies of SIGPIPE, which pipefail turns into a failed probe. So the
      # producer's whole output is captured first.
      probe_has() {
        local pattern="$1" out
        shift
        out="$("$@" 2>/dev/null || true)"
        grep -q -- "$pattern" <<<"$out"
      }

      install_claude() {
        if [ -z "$(find "${claudeVersionsDir}" -maxdepth 1 -type f -perm -u+x -print -quit 2>/dev/null || true)" ]; then
          curl -fsSL https://claude.ai/install.sh | bash -s latest
        fi
        # Unconditional: the guarantee upstream documents covers updates, not
        # the initial `claude install` the installer script runs.
        ${lib.getExe claudeLauncherKeeper}
      }

      # No tty under systemd, so pi's installer takes its defaults and never
      # edits an rc file. `pi update` self-updates. The boot-time L+ link has
      # already replaced an npm pi's bin link, so the installer cannot see it
      # to migrate; the npm package is removed here instead.
      install_pi() {
        [ -x "$HOME/.pi/agent/bin/pi" ] \
          || curl -fsSL https://pi.dev/install.sh | bash
        [ ! -d "$HOME/.local/lib/node_modules/@earendil-works/pi-coding-agent" ] \
          || npm uninstall -g @earendil-works/pi-coding-agent
        link_bin pi "$HOME/.pi/agent/bin/pi"
      }

      # The integration writes into pi's extensions directory, so it needs pi
      # installed first. Its status probe, not a file test, decides whether it
      # is current: see nix/harnesses.md#herdrs-pi-integration.
      install_herdr_pi() {
        probe_has '^pi: current' herdr integration status || herdr integration install pi
      }

      phase claude install_claude
      phase pi install_pi
      phase herdr-pi install_herdr_pi

      for tool in claude pi node; do
        phase "$tool --version" "$tool" --version
      done

      if [ "''${#failed[@]}" -gt 0 ]; then
        printf 'yolobox-harness-install: failed phase: %s\n' "''${failed[@]}" >&2
        exit 1
      fi
    '';
  };

  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = cfg.herdr; };
in
{
  # The option was one hash for the one guest system; a box on either CPU needs
  # one per system. An old local.nix that still sets it must fail with this text,
  # not with "option does not exist".
  imports = [
    (lib.mkRemovedOptionModule [ "yolobox" "harness" "herdr" "hash" ]
      "use yolobox.harness.herdr.hashes = { <system> = \"sha256-...\"; }, one SRI hash per guest system (aarch64-linux, x86_64-linux)")
  ];

  options.yolobox.harness = {
    herdr = {
      # Defaulting to null means the guest tracks nixpkgs' herdr, so the
      # version is not a hand-copied duplicate of a host fact that nobody
      # remembers to re-copy.
      #
      # The invariant it has to satisfy: `herdr machine add` gates a saved
      # connection on capability, not exact version equality — the guest
      # server's endpoint_protocol_generation has to match the Mac
      # client's own constant exactly, coarser than the version string
      # (see nix/herd-server.md#capability-gating).
      #
      # Setting version+hashes here re-pins the guest to a GitHub release
      # (nix/pkgs/herdr-bin.nix) — the escape hatch for when nixpkgs lags a
      # herdr release the Mac has already taken.
      version = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "herdr release fetched via nix/pkgs/herdr-bin.nix, bypassing nixpkgs' pin.";
      };
      hashes = lib.mkOption {
        type = lib.types.nullOr (lib.types.attrsOf lib.types.str);
        default = null;
        description = "SRI hash of the herdr release binary at yolobox.harness.herdr.version, per guest system (aarch64-linux, x86_64-linux). Setting this activates the override.";
      };
    };
  };

  config = {
    environment.systemPackages = [
      # pi shells out to npm to install the packages listed in its settings.
      pkgs.nodejs
      herdrPkg
    ];

    # A copy, not a store symlink: the link in the agent's home must point at a
    # path that survives every rebuild, so that the keeper below can compare
    # `readlink` against one constant.
    environment.etc."yolobox/bin/claude" = {
      source = lib.getExe claudeLauncher;
      mode = "0555";
    };

    systemd.tmpfiles.rules = homeTmpfiles {
      home = homeDir;
      user = agentUser;
      dirs = [ ".local" ".local/bin" ];
      links = [
        { path = ".local/bin/claude"; argument = launcherPath; }
        { path = ".local/bin/pi"; argument = "${homeDir}/.pi/agent/bin/pi"; }
      ];
    };

    # tmpfiles only fires at boot and on switch, which is a window wide enough
    # for a self-updating claude to install its own launcher over ours and go
    # unnoticed until the next rebuild. This closes it at the moment it opens.
    # The service writes only when something is actually wrong, so the write it
    # makes retriggers the watch exactly once and then settles.
    systemd.user.paths.yolobox-claude-launcher = {
      description = "Watch the agent's claude launcher and version store";
      unitConfig.ConditionUser = agentUser;
      wantedBy = [ "paths.target" ];
      pathConfig.PathModified = [ "%h/.local/bin" "%h/.local/share/claude/versions" ];
    };
    # The path unit only fires on a change, and a version too fresh to prune
    # then becomes prunable with nothing left to change, so the same service
    # also runs hourly.
    systemd.user.timers.yolobox-claude-launcher = {
      description = "Periodically prune the agent's old claude versions";
      unitConfig.ConditionUser = agentUser;
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "hourly";
        Persistent = true;
      };
    };
    # StartLimitIntervalSec = 0 because the path unit above fires on every
    # write into the version store, and one ~330 MB download is thousands of
    # them: systemd's default burst limit would trip and leave the path unit
    # failed.
    systemd.user.services.yolobox-claude-launcher = {
      description = "Re-assert the box's claude launcher and prune old versions";
      unitConfig = {
        ConditionUser = agentUser;
        StartLimitIntervalSec = 0;
      };
      serviceConfig = {
        Type = "oneshot";
        ExecStart = lib.getExe claudeLauncherKeeper;
      };
    };

    # The system half of the install's gate. dhcpcd is configured with `waitip`
    # and forks only once the lease is applied and resolvconf has written the
    # resolver, so "dhcpcd.service started" is the event "the network works".
    # Nothing else pulls in network-online.target, so this unit wants dhcpcd
    # itself. RemainAfterExit keeps the flag for the whole boot; the stop
    # removes it, so a flag never outlives the network state it describes.
    systemd.services.yolobox-network-ready = {
      description = "Flag that the network is up, for the agent's install unit";
      wantedBy = [ "multi-user.target" ];
      wants = [ "dhcpcd.service" ];
      after = [ "dhcpcd.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${lib.getExe' pkgs.coreutils "touch"} ${networkReadyFlag}";
        ExecStop = "${lib.getExe' pkgs.coreutils "rm"} -f ${networkReadyFlag}";
      };
    };

    # The user half: the install starts when the flag appears (inotify, no
    # polling). The agent's user manager starts at boot, in parallel with the
    # network, and an install that ran first would fail on DNS and wait out
    # its RestartSec. The service has no wantedBy of its own: this path unit
    # is what starts it.
    systemd.user.paths.yolobox-harness-install = {
      description = "Start the harness install once the network is up";
      unitConfig.ConditionUser = agentUser;
      wantedBy = [ "default.target" ];
      pathConfig.PathExists = networkReadyFlag;
    };

    # The harnesses install themselves from their vendors, so the box needs no
    # nixpkgs version of any of them — and gets their self-updates for free.
    # StartLimitIntervalSec = 0 because a vendor outage or a dropped connection
    # mid-download must keep being retried by Restart past systemd's default
    # burst limit.
    systemd.user.services.yolobox-harness-install = {
      description = "Install claude and pi from their vendors";
      unitConfig = {
        ConditionUser = agentUser;
        StartLimitIntervalSec = 0;
      };
      after = [ "yolobox-claude-launcher.path" ];
      # "${homeDir}/.local" first so `claude` here is the launcher, and so pi
      # (through the box's ~/.local/bin/pi link) resolves as soon as it lands.
      path = [
        "${homeDir}/.local"
        pkgs.bash
        pkgs.coreutils
        pkgs.curl
        pkgs.gnutar
        pkgs.gzip
        pkgs.zstd
        pkgs.git
        pkgs.nodejs
        pkgs.findutils
        pkgs.gawk
        pkgs.gnugrep
        herdrPkg
      ];
      # NIX_LD/NIX_LD_LIBRARY_PATH are spelled out rather than inherited: this
      # unit must not depend on PAM having reached the user manager with a
      # login environment. The vendor-downloaded binaries (claude, pi) are
      # dynamically linked glibc programs and need them at
      # install time, where the version checks run them.
      environment = {
        NPM_CONFIG_PREFIX = "${homeDir}/.local";
      } // nixLdEnv;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        Restart = "on-failure";
        RestartSec = 20;
        TimeoutStartSec = "20min";
        ExecStart = lib.getExe harnessInstall;
      };
    };

    assertions = [
      {
        assertion = (cfg.herdr.version == null) == (cfg.herdr.hashes == null);
        message = "yolobox.harness.herdr: version and hashes must both be null or both be set.";
      }
    ];
  };
}
