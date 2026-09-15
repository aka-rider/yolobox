{ config, lib, pkgs, agentUser, ... }:
let
  cfg = config.yolobox.harness;

  claudeHooksFile = import ./lib/claude-hooks-file.nix;
  homeDir = config.users.users.${agentUser}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
  nixLdEnv = import ./lib/nix-ld-env.nix;

  launcherPath = "/etc/yolobox/bin/claude";
  launcherLink = "${homeDir}/.local/bin/claude";
  claudeVersionsDir = "${homeDir}/.local/share/claude/versions";

  # Claude Code >= 2.1.207 documents that a custom ~/.local/bin/claude is left
  # alone: `claude update` and the background updater only drop new binaries
  # into ~/.local/share/claude/versions/. That guarantee is the whole delivery
  # channel for claude's settings file (nix/display.nix), which reaches
  # claude as `--settings <file>` and nowhere else (see nix/display.nix for
  # why the enterprise settings tier can't carry it). The box therefore OWNS
  # that path with a launcher of its own rather than putting a wrapper on the
  # system PATH and racing it: the agent's dotfiles prepend $HOME/.local/bin,
  # so any wrapper behind it loses in every interactive shell, silently,
  # which is exactly how 657fc21 ended herd reporting box-wide within a day.
  #
  # --settings sits before "$@" so a user's own later --settings still wins.
  claudeLauncher = pkgs.writeShellApplication {
    name = "claude";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils pkgs.git ];
    text = ''
      versions="${claudeVersionsDir}"
      newest="$(find "$versions" -maxdepth 1 -type f -perm -u+x -printf '%f\n' 2>/dev/null | sort -V | tail -n 1 || true)"

      if [ -z "$newest" ]; then
        echo "claude: no installed version under $versions" >&2
        echo "  the vendor install runs as a user unit: systemctl --user status yolobox-harness-install" >&2
        echo "  its output: journalctl --user -u yolobox-harness-install" >&2
        exit 127
      fi

      # The playwright plugin's stdio server inherits this process's
      # environment and reads PLAYWRIGHT_MCP_OUTPUT_DIR once at start, so the
      # launcher is the only place left that can make it per project. The
      # project comes from the logical cwd, not git's physical toplevel:
      # ~/wrk is a symlink to ~/Developer, and the repo mirrors logical
      # paths, so show-toplevel would resolve the symlink away.
      cdup="$(git rev-parse --show-cdup 2>/dev/null || true)"
      top="$(cd "$PWD/$cdup" 2>/dev/null && pwd -L || printf '%s' "$PWD")"
      default_output_dir="$HOME/artifacts"
      output_dir="''${PLAYWRIGHT_MCP_OUTPUT_DIR:-$default_output_dir}"
      case "$top" in
        "$HOME"/*)
          project="''${top#"$HOME/"}"
          project="''${project//\//--}"
          if [ "$output_dir" = "$default_output_dir" ]; then
            output_dir="$default_output_dir/$project"
          fi
          ;;
      esac
      export PLAYWRIGHT_MCP_OUTPUT_DIR="$output_dir"
      mkdir -p "$output_dir"

      # herdr's classification keys off the foreground process name, but the
      # exec'd binary here is named after its version directory (e.g.
      # "2.1.268"), not "claude" - so herdr never recognises it without help.
      export HERDR_AGENT=claude
      exec "$versions/$newest" --settings ${claudeHooksFile.path} "$@"
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
      older="$(find "${claudeVersionsDir}" -maxdepth 1 -type f -perm -u+x -printf '%f\n' 2>/dev/null | sort -V | head -n "-$keep_versions" || true)"
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

      if [ -z "$(find "${claudeVersionsDir}" -maxdepth 1 -type f -perm -u+x -print -quit 2>/dev/null || true)" ]; then
        curl -fsSL https://claude.ai/install.sh | bash -s latest
      fi
      # Unconditional: the guarantee upstream documents covers updates, not the
      # initial `claude install` the installer script runs.
      ${lib.getExe claudeLauncherKeeper}

      claude plugin marketplace list 2>/dev/null | grep -q claude-plugins-official \
        || claude plugin marketplace add anthropics/claude-plugins-official
      for plugin in context7 playwright; do
        claude plugin list 2>/dev/null | grep -q "$plugin@claude-plugins-official" \
          || claude plugin install "$plugin@claude-plugins-official" --scope user
      done

      command -v pi >/dev/null || npm install -g --ignore-scripts @earendil-works/pi-coding-agent

      # t3code's own vendor install; it never edits an rc file. Its installer
      # unpacks straight into the layout `t3 service install` also uses, so
      # this download is reused rather than repeated.
      [ -x "$HOME/.local/bin/t3" ] || curl -fsSL https://t3.codes/install.sh | bash
      [ -f "$HOME/.config/systemd/user/t3code.service" ] || t3 service install

      # Scripts ON, unlike pi: agent-browser's postinstall IS the download of
      # its prebuilt binary. That download fails silently, so the version call
      # below is the hard check rather than a courtesy. Pinned to the version
      # pi-agent-browser-native 0.8.2 recommends; it accepts 0.35.0 and newer,
      # and refuses browser-backed calls below that floor, at call time, not here.
      command -v agent-browser >/dev/null || npm install -g agent-browser@0.38.1
      agent-browser --version

      for package in @upstash/context7-pi pi-agent-browser-native; do
        pi list 2>/dev/null | grep -q "$package" || pi install "npm:$package"
      done

      herdr integration status | grep -q '^pi: current' || herdr integration install pi

      # pi-agent-browser-native ships a `pi-agent-browser-config` helper, but
      # its own README states that `pi install npm:...` does not put it on
      # PATH and names writing this file as the equivalent route. The shape is
      # the helper's own: {version:1, browser:{executablePath}}. Merged with
      # jq rather than overwritten, because the same file also holds the
      # extension's webSearch settings.
      browser_config_dir="$HOME/.pi/config/pi-agent-browser-native"
      browser_config="$browser_config_dir/config.json"
      mkdir -p "$browser_config_dir"
      [ -f "$browser_config" ] || echo '{}' > "$browser_config"
      jq --arg path "${lib.getExe pkgs.chromium}" \
        '.version = 1 | .browser.executablePath = $path' \
        "$browser_config" > "$browser_config.tmp"
      mv -f "$browser_config.tmp" "$browser_config"

      # --no-modify-path: no installer may edit the agent's rc files. The
      # opencode installer has no install-dir override, hence the fixed link.
      [ -x "$HOME/.opencode/bin/opencode" ] \
        || curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path
      ln -sfn "$HOME/.opencode/bin/opencode" "${homeDir}/.local/bin/opencode.tmp"
      mv -T "${homeDir}/.local/bin/opencode.tmp" "${homeDir}/.local/bin/opencode"

      claude --version
      pi --version
      t3 --version
      opencode --version
      agent-browser --version
      node --version
    '';
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
      # The invariant it has to satisfy: `herdr machine add` gates a saved
      # connection on capability, not exact version equality — the guest
      # server's endpoint_protocol_generation has to match the Mac
      # client's own constant exactly, coarser than the version string
      # (see CLAUDE.md, "herdr: the VM runs its own server, panes are real
      # ptys").
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
        type = lib.types.nullOr (lib.types.attrsOf lib.types.str);
        default = null;
        description = "SRI hash of the herdr release binary at yolobox.harness.herdr.version, keyed by nix system (e.g. \"x86_64-linux\"). Setting this activates the override.";
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

    # The trailing "r" rules clear the links a retired MCP layer left behind:
    # a dropped L+ link is not removed by the rebuild that drops it.
    systemd.tmpfiles.rules = homeTmpfiles {
      home = homeDir;
      dirUser = agentUser;
      dirs = [ ".local" ".local/bin" ];
      links = [
        { path = ".local/bin/claude"; argument = launcherPath; }
        { path = ".local/bin/opencode"; argument = "${homeDir}/.opencode/bin/opencode"; }
      ];
    } ++ [
      "r ${homeDir}/.pi/agent/extensions/pi-mcp-adapter"
      "r ${homeDir}/.pi/agent/skills/mcp-scripting"
      "r ${homeDir}/.config/mcp/mcp.json"
      "r ${homeDir}/.pi/agent/extensions/yolobox-agent-state.js"
    ];

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
    systemd.user.services.yolobox-claude-launcher = {
      description = "Re-assert the box's claude launcher and prune old versions";
      unitConfig.ConditionUser = agentUser;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = lib.getExe claudeLauncherKeeper;
      };
    };

    # The harnesses install themselves from their vendors, so the box needs no
    # nixpkgs version of any of them — and gets their self-updates for free.
    # StartLimitIntervalSec = 0 because the only expected failure is "no
    # network yet", which Restart must keep retrying past systemd's default
    # burst limit.
    systemd.user.services.yolobox-harness-install = {
      description = "Install claude, pi, opencode and their plugins from their vendors";
      unitConfig = {
        ConditionUser = agentUser;
        StartLimitIntervalSec = 0;
      };
      wantedBy = [ "default.target" ];
      after = [ "yolobox-claude-launcher.path" ];
      # "${homeDir}/.local" first so `claude` here is the launcher, and so the
      # npm-installed pi and agent-browser resolve as soon as they land.
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
        pkgs.gnugrep
        pkgs.jq
        herdrPkg
      ];
      # NIX_LD/NIX_LD_LIBRARY_PATH are spelled out rather than inherited: this
      # unit must not depend on PAM having reached the user manager with a
      # login environment. t3's own downloaded binary needs them too, at
      # install time (the vendor script runs `t3 --version` to verify the
      # download) and every time t3code.service execs it (nix/t3.nix).
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
        assertion = (cfg.herdr.version == null) == (cfg.herdr.hash == null);
        message = "yolobox.harness.herdr: version and hash must both be null or both be set.";
      }
      {
        assertion = cfg.herdr.hash == null || lib.hasAttr pkgs.stdenv.hostPlatform.system cfg.herdr.hash;
        message = "yolobox.harness.herdr.hash has no entry for ${pkgs.stdenv.hostPlatform.system}.";
      }
    ];
  };
}
