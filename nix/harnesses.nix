{ lib, pkgs, yolobox, facts, ... }:
let
  inherit (yolobox) agentHomeTmpfiles agentUnit herdrPkg;

  launcherEtc = "yolobox/bin/claude";
  launcherPath = "/etc/${launcherEtc}";
  launcherHomePath = ".local/bin/claude";
  launcherLink = "${facts.agent_home}/${launcherHomePath}";
  claudeVersionsDir = "${facts.agent_home}/.local/share/claude/versions";

  # The one definition of an installed claude version; the launcher, the
  # keeper and the installer all ask this script.
  claudeVersions = pkgs.writeShellApplication {
    name = "yolobox-claude-versions";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    runtimeEnv = {
      CLAUDE_VERSIONS_DIR = claudeVersionsDir;
      CLAUDE_VERSION_RE = "[0-9]+(\\.[0-9]+)*(-[A-Za-z0-9.]+)?";
    };
    text = builtins.readFile ./harnesses/yolobox-claude-versions.sh;
  };

  # Exists while the system's network is up. A user unit cannot order itself
  # after a system unit, so the system side drops this flag and a user path
  # unit waits for it.
  networkReadyFlag = "/run/yolobox-network-ready";

  # The scripts live in nix/harnesses/, shellchecked at build time. The
  # launcher takes no runtimeInputs on purpose: it execs claude, whose children
  # must keep the PATH the project's shell gave them.
  claudeLauncher = pkgs.writeShellApplication {
    name = "claude";
    runtimeInputs = [ ];
    runtimeEnv = {
      CLAUDE_VERSIONS_DIR = claudeVersionsDir;
      CLAUDE_VERSIONS_LIST = lib.getExe claudeVersions;
      YOLOBOX_AGENT_USER = facts.agent_user;
    };
    text = builtins.readFile ./harnesses/claude.sh;
  };

  claudeLauncherKeeper = pkgs.writeShellApplication {
    name = "yolobox-claude-launcher-keep";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    runtimeEnv = {
      CLAUDE_LAUNCHER = launcherPath;
      CLAUDE_LAUNCHER_LINK = launcherLink;
      CLAUDE_VERSIONS_DIR = claudeVersionsDir;
      CLAUDE_VERSIONS_LIST = lib.getExe claudeVersions;
    };
    text = builtins.readFile ./harnesses/yolobox-claude-launcher-keep.sh;
  };

  harnessInstall = pkgs.writeShellApplication {
    name = "yolobox-harness-install";
    runtimeEnv.CLAUDE_VERSIONS_LIST = lib.getExe claudeVersions;
    text = builtins.readFile ./harnesses/yolobox-harness-install.sh;
  };
in
{
  environment.systemPackages = [
    # pi shells out to npm to install the packages listed in its settings.
    pkgs.nodejs
    herdrPkg
  ];

  # A copy, not a store symlink: the link in the agent's home must point at a
  # path that survives every rebuild, so that the keeper can compare
  # `readlink` against one constant.
  environment.etc.${launcherEtc} = {
    source = lib.getExe claudeLauncher;
    mode = "0555";
  };

  systemd.tmpfiles.rules = agentHomeTmpfiles {
    dirs = [ ".local" ".local/bin" ];
    links = [
      { path = launcherHomePath; argument = launcherPath; }
      { path = ".local/bin/pi"; argument = "${facts.agent_home}/.pi/agent/bin/pi"; }
    ];
  };

  # Re-asserts the launcher the moment a self-updating claude replaces it;
  # tmpfiles only fires at boot and on switch. PathChanged, not PathModified:
  # one ~330 MB download is thousands of writes, past the path unit's trigger
  # limit of 200 per 2 s.
  systemd.user.paths.yolobox-claude-launcher = agentUnit {
    description = "Watch the agent's claude launcher and version store";
    wantedBy = [ "paths.target" ];
    pathConfig.PathChanged = [ "%h/.local/bin" "%h/.local/share/claude/versions" ];
  };
  # The path unit only fires on a change, and a version too fresh to prune
  # then becomes prunable with nothing left to change, so the same service
  # also runs hourly.
  systemd.user.timers.yolobox-claude-launcher = agentUnit {
    description = "Periodically prune the agent's old claude versions";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "hourly";
      Persistent = true;
    };
  };
  # StartLimitIntervalSec = 0: the path unit starts this on every file event in
  # two directories, its own relink and prune included, and a burst past
  # systemd's default five starts in ten seconds would fail the path unit.
  systemd.user.services.yolobox-claude-launcher = agentUnit {
    description = "Re-assert the box's claude launcher and prune old versions";
    unitConfig.StartLimitIntervalSec = 0;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = lib.getExe claudeLauncherKeeper;
    };
  };

  # The system half of the install's gate. dhcpcd is configured with `waitip`
  # and forks only once the lease is applied and resolvconf has written the
  # resolver, so "dhcpcd.service started" is the event "the network works".
  # Nothing else pulls in network-online.target, so this unit wants dhcpcd
  # itself. RemainAfterExit keeps the flag while dhcpcd runs; bindsTo stops
  # this unit, and its stop removes the flag, when dhcpcd stops or dies, so
  # the flag never outlives the network state it describes. A dhcpcd
  # restarted by hand does not bring it back: the install it gates has run
  # by then, or retries on its own.
  systemd.services.yolobox-network-ready = {
    description = "Flag that the network is up, for the agent's install unit";
    wantedBy = [ "multi-user.target" ];
    wants = [ "dhcpcd.service" ];
    bindsTo = [ "dhcpcd.service" ];
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
  systemd.user.paths.yolobox-harness-install = agentUnit {
    description = "Start the harness install once the network is up";
    wantedBy = [ "default.target" ];
    pathConfig.PathExists = networkReadyFlag;
  };

  # The harnesses install themselves from their vendors, so the box needs no
  # nixpkgs version of any of them, and gets their self-updates for free.
  # StartLimitIntervalSec = 0 because a vendor outage or a dropped connection
  # mid-download must keep being retried by Restart past systemd's default
  # burst limit.
  systemd.user.services.yolobox-harness-install = agentUnit {
    description = "Install claude and pi from their vendors";
    unitConfig.StartLimitIntervalSec = 0;
    after = [ "yolobox-claude-launcher.path" ];
    # ~/.local first so `claude` here is the launcher, and so pi
    # (through the box's ~/.local/bin/pi link) resolves as soon as it lands.
    path = [
      "${facts.agent_home}/.local"
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
      claudeLauncherKeeper
    ];
    # NIX_LD and NIX_LD_LIBRARY_PATH are spelled out rather than inherited:
    # this unit must not depend on PAM having reached the user manager with a
    # login environment, and the vendor-downloaded claude and pi are
    # dynamically linked glibc programs, which run here for their version
    # checks.
    environment = {
      NPM_CONFIG_PREFIX = "${facts.agent_home}/.local";
      NIX_LD = "/run/current-system/sw/share/nix-ld/lib/ld.so";
      NIX_LD_LIBRARY_PATH = "/run/current-system/sw/share/nix-ld/lib";
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Restart = "on-failure";
      RestartSec = 20;
      TimeoutStartSec = "20min";
      ExecStart = lib.getExe harnessInstall;
    };
  };

  # Reachable by name, so CI can build the packages on their own: the check
  # phase (shellcheck) runs only in a build.
  system.build.yoloboxClaudeVersions = claudeVersions;
  system.build.yoloboxClaudeLauncher = claudeLauncher;
  system.build.yoloboxClaudeLauncherKeep = claudeLauncherKeeper;
  system.build.yoloboxHarnessInstall = harnessInstall;
}
