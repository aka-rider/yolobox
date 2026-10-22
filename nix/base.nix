{ config, pkgs, modulesPath, username, agentUser, lib, version, ... }:
let
  agentEnv = import ./lib/agent-env.nix {
    agentHome = config.users.users.${agentUser}.home;
  };

  opProxy = { name, listen, target }: {
    systemd.sockets.${name} = {
      description = "Agent-owned socket proxied to the operator's ${name} forward";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = listen;
        SocketUser = agentUser;
        SocketGroup = "users";
        SocketMode = "0600";
      };
    };
    systemd.services.${name} = {
      description = "Proxy the ${name} socket to its operator-owned forward";
      requires = [ "${name}.socket" ];
      after = [ "${name}.socket" ];
      serviceConfig = {
        User = username;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd /run/yolobox-op/${target}";
      };
    };
  };
  # Where the agent's own direnv config lives: agent-env.nix exports it as
  # DIRENV_CONFIG for agent processes only.
  agentDirenvDir = lib.removePrefix "/etc/" agentEnv.env.DIRENV_CONFIG;
  agentHome = config.users.users.${agentUser}.home;

  opProxies = map opProxy [
    { name = "yolobox-op-agent"; listen = agentEnv.env.SSH_AUTH_SOCK; target = "agent.sock"; }
    { name = "yolobox-aws-broker"; listen = agentEnv.awsBrokerSock; target = "aws-broker.sock"; }
  ];
in
{
  # Lets someone drop a package or setting into the running box without a
  # checkout or push: the guest no longer carries a copy of this repo, so
  # the old "edit nix/base.nix in the VM and rebuild" workflow needs a
  # replacement.
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ] ++ lib.optional (builtins.pathExists /etc/yolobox/local.nix) /etc/yolobox/local.nix
    ++ opProxies;

  # /boot is the ESP itself, as the shipped nixos-lima image mounts it —
  # matching the image is what keeps the first `nixos-rebuild switch` on a
  # fresh instance from having to migrate anything. The cost is that vda1 is
  # 249 MiB (make-disk-image's bootSize default, baked into the prebuilt
  # qcow2 and not overridable from here), and a kernel set is ~92 MiB on top
  # of ~14 MiB of GRUB, so two sets fit and three never do.
  # linuxPackages_latest brings a new set every few weeks, and a full ESP
  # fails late and ugly: the closure builds, the system profile advances,
  # then activation dies installing the bootloader with "No space left on
  # device" — the box still booted on the old generation while the profile
  # claims otherwise. install-grub.pl copies every retained generation's
  # kernel before it unlinks any obsolete one, so peak occupancy is retained
  # plus dropped, and one is the only limit whose steady-state peak — the
  # retained set alongside the incoming one — fits. Keeping a rollback
  # generation across a kernel bump is thus impossible at this ESP size; on
  # a disposable box that is a fair trade.
  boot.loader.grub = {
    device = "nodev";
    efiSupport = true;
    efiInstallAsRemovable = true;
    configurationLimit = 1;
  };
  fileSystems."/boot" = {
    device = "/dev/vda1"; # /dev/disk/by-label/ESP
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    autoResize = true;
    fsType = "ext4";
    options = [ "noatime" "nodiratime" "discard" ];
  };
  # The two halves of growing the root disk, and they only work together:
  # autoResize above grows the ext4 filesystem into vda2, this grows vda2
  # into whatever the disk has become. Upstream orders the growpart oneshot
  # before systemd-growfs-root, so a boot after `limactl edit --disk` picks
  # up the extra space in that order and needs nothing else; SuccessExitStatus
  # "0 1" makes the already-grown case a no-op on every later boot. Without
  # this the bump is silent: the backing file gets bigger, the guest boots
  # clean, vda2 stays where it was and df is unchanged. Only vda2 moves —
  # vda1 ends at the sector vda2 starts on, so the ESP above is unaffected.
  boot.growPartition = true;
  boot.kernelPackages = pkgs.linuxPackages_latest;
  # Same-uid ptrace (py-spy, gdb, strace -p). The uid split, not Yama, is the
  # boundary; see CLAUDE.md "Debuggers: same-uid ptrace".
  boot.kernel.sysctl."kernel.yama.ptrace_scope" = 0;

  swapDevices = [{ device = "/var/swapfile"; size = 16384; }];

  # A crash-looping language server (marksman, 2026-10-05..18, missing ICU) filled
  # /var/lib/systemd/coredump with 2.1G of dumps nobody read; systemd's
  # default cap is 10% of the filesystem, ~40G here. `yo gc --yes` empties it.
  systemd.coredump.settings.Coredump.MaxUse = "1G";

  services.lima.enable = true;
  # nixos-lima declares lima-init and lima-guestagent as ordinary NixOS
  # units, so a nixpkgs bump alone rehashes the store paths their
  # Environment= and ExecStart= embed and switch-to-configuration reads that
  # as "restart". Restarting the guestagent is not an ordinary bounce: lima
  # 2.2.0 serves the event stream and the TCP Tunnel RPC from one
  # grpc.ClientConn, and on reconnect it replaces that conn but never
  # refreshes the dialer an existing listener already captured, so every
  # forwarded port accepts and then resets until the host agent itself is
  # restarted. That behaviour began with lima PR #4889 in v2.1.2, which
  # closes the stale conn on reconnect. Both units must be pinned, not just
  # the guestagent: systemd propagates a restart of lima-init to the
  # guestagent through its Requires=. mkForce only on lima-init,
  # which upstream already assigns explicitly. New guestagent code lands on
  # the next boot, which is when the host agent is recreated anyway.
  systemd.services.lima-guestagent.restartIfChanged = false;
  systemd.services.lima-init.restartIfChanged = lib.mkForce false;

  # Upstream's user@.service ships OOMScoreAdjust=100, making the per-user
  # manager a preferred kernel-OOM victim over every system service; killing
  # it takes down every rootless podman container at once.
  systemd.services."user@" = {
    overrideStrategy = "asDropin";
    serviceConfig.OOMScoreAdjust = -500;
  };

  networking.hostName = "yolobox";
  users.mutableUsers = true;
  system.stateVersion = "25.11";

  # This is the operator's account — the human who drives the Mac, not any
  # agent. Takes over lima-init's existence-guarded useradd from first boot
  # (Gotcha 1) — including lima >=2.1.0's ".guest"-suffixed home
  # (lima-vm/lima#4578). That account is named after the host Mac user
  # (`username`, threaded in impurely — see flake.nix), not a fixed name:
  # there is no separate "yolobox account", this declares the same account
  # cidata already created so nix and interactive SSH sessions share one
  # $HOME. NixOS refuses isNormalUser below uid 1000 (lima's cidata UID
  # here is 501, matching the host's macOS UID), so this uses isSystemUser
  # instead. It keeps wheel and runs no agent; rootless podman's
  # autoSubUidGidRange lives on the agent account below instead.
  users.users.${username} = {
    isSystemUser = true;
    group = "users";
    uid = 501;
    home = "/home/${username}.guest";
    extraGroups = [ "wheel" ];
    shell = pkgs.zsh;
  };

  # The agent's account. uid must not be 501: a second account sharing
  # lima's uid is an outage this repo already paid for once (see CLAUDE.md,
  # "Two accounts: the operator mirrors the host, the agent does not") — passwd lookups resolve a uid to
  # whichever entry comes first, so two accounts at 501 are indistinguishable
  # to whoami/SUDO_USER while modules silently write state into the wrong
  # home. It is deliberately not lima's cidata account either, because
  # lima-init runs `usermod -a -G wheel $LIMA_CIDATA_USER` unconditionally on
  # every boot, after activation — removing wheel from that account here
  # would not stick. The agent is in no supplementary group on purpose,
  # `systemd-journal` included: that group reads the system journal, where
  # the operator's `sudo ... COMMAND=<argv>` lines live. xvfb and openbox
  # therefore run as agent user units (nix/display.nix), whose logs sit in
  # the agent's own user journal, which it owns without any group.
  users.users.${agentUser} = {
    isNormalUser = true;
    uid = 1000;
    group = "users";
    home = "/home/${agentUser}";
    shell = pkgs.zsh;
    autoSubUidGidRange = true;
    # Locked, so a hand-run `passwd` cannot turn this account into a password
    # login; ssh keys are its only way in.
    hashedPassword = "!";
    # The agent's user manager must run from boot, not from the first login:
    # it is what installs the harnesses, and nothing may wait for `yo enter`.
    linger = true;
  };

  users.users.root.hashedPassword = "!";

  # With mutableUsers the pinned update-users-groups.pl applies hashedPassword
  # only when it creates an account, so the declarations above lock nothing on
  # a box whose accounts already exist (a hand-run `sudo passwd root` would
  # stick). This enforces the lock on every activation. A field already
  # starting with ! or * is locked and left alone; `passwd -l` would prefix
  # another !. A missing shadow entry fails the activation outright.
  system.activationScripts.lockRootAndAgent = {
    deps = [ "users" ];
    text = ''
      for account in root ${agentUser}; do
        entry="$(${pkgs.getent}/bin/getent shadow "$account")" || { echo "lockRootAndAgent: no shadow entry for $account" >&2; exit 1; }
        field="''${entry#*:}"; field="''${field%%:*}"
        case "$field" in
          '!'*|'*'*) ;;
          *) ${pkgs.shadow}/bin/passwd -l "$account" ;;
        esac
      done
    '';
  };

  # username reaches shell scripts, sshd config and tmpfiles rules below, and
  # must not be able to collide with or impersonate the agent.
  assertions = [
    {
      assertion = username != agentUser;
      message = "YOLOBOX_USERNAME is \"${username}\", the agent's own account name; the operator and the agent must be different accounts.";
    }
    {
      assertion = username != "root";
      message = "YOLOBOX_USERNAME is root; the operator mirrors the Mac user, never root.";
    }
    {
      assertion = builtins.match "[a-z_][a-z0-9_-]*" username != null;
      message = "YOLOBOX_USERNAME \"${username}\" is not a plain lowercase unix name ([a-z_][a-z0-9_-]*).";
    }
  ];

  services.openssh.enable = true;
  # Lima's hostagent waits for, and forwards to, TCP 22 on 192.168.5.15
  # (enp0s1, lima's NAT network, unreachable from the LAN), so 22 is open there
  # and on tailscale0 (nix/tailscale.nix) and nowhere else.
  services.openssh.openFirewall = false;
  networking.firewall.interfaces.enp0s1.allowedTCPPorts = [ 22 ];
  services.openssh.settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
    PermitRootLogin = "prohibit-password";
  };
  # The default (~agent/.ssh/authorized_keys) must not be honoured: it is
  # agent-writable, so the agent could mint its own persistent login.
  # lima-init writes /etc/ssh/authorized_keys.d/${username} every boot
  # instead, root-owned 0600 in a root-owned 0700 directory. sshd opens an
  # AuthorizedKeysFile with the *target user's* privileges, so pointing the
  # agent at that file denies every login; only a command run as root can
  # read it. The command must live outside /nix/store: sshd's safe_path
  # rejects any path with a group-writable component and the store is 1775,
  # which is why this is a copied /etc file (a non-symlink mode makes
  # environment.etc copy it) and not a store path.
  environment.etc."ssh/agent-authorized-keys" = {
    mode = "0555";
    text = ''
      #!${pkgs.runtimeShell}
      exec ${pkgs.coreutils}/bin/cat ${lib.escapeShellArg "/etc/ssh/authorized_keys.d/${username}"}
    '';
  };
  services.openssh.extraConfig = ''
    Match User ${agentUser}
      AuthorizedKeysFile none
      AuthorizedKeysCommand /etc/ssh/agent-authorized-keys
      AuthorizedKeysCommandUser root
  '';

  systemd.tmpfiles.rules = [
    "d /run/yolobox-op 0700 ${username} users -"
    # ~/wrk is the Mac's spelling of ~/Developer, and the mirror carries it
    # verbatim. Plain L, never L+: L+ would delete a real ~/wrk directory.
    "d ${config.users.users.${agentUser}.home}/Developer 0755 ${agentUser} users -"
    "L ${config.users.users.${agentUser}.home}/wrk - - - - Developer"
  ];

  environment.localBinInPath = true;

  environment.extraInit = ''
    if [ "$(id -un)" = ${lib.escapeShellArg agentUser} ]; then
      ${lib.concatStringsSep "\n" (lib.mapAttrsToList
        (name: value: "export ${name}=${lib.escapeShellArg value}")
        agentEnv.env)}
    fi
  '';

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;

  # The module's DIRENV_CONFIG=/etc/direnv is what root and the operator get,
  # and it carries no whitelist: an .envrc is arbitrary code the agent
  # writes, and auto-trusting it in the operator's shell would hand over the
  # operator's sudo on the first cd. The agent alone gets /etc/direnv-agent
  # (DIRENV_CONFIG in nix/lib/agent-env.nix, which also reaches herdr panes
  # and t3), whose direnv.toml whitelists its home. direnv reads direnvrc and
  # lib/*.sh from the same directory, so those two are the /etc/direnv files
  # themselves, keeping nix-direnv. An XDG ~/.config file is never read.
  programs.direnv.enable = true;
  environment.etc."${agentDirenvDir}/direnv.toml".source =
    (pkgs.formats.toml { }).generate "direnv.toml" { whitelist.prefix = [ agentHome ]; };
  environment.etc."${agentDirenvDir}/direnvrc".source = config.environment.etc."direnv/direnvrc".source;
  environment.etc."${agentDirenvDir}/lib/zz-user.sh".source = config.environment.etc."direnv/lib/zz-user.sh".source;

  # .NET binaries, such as pi-lens's downloaded marksman, abort without ICU
  # ("Couldn't find a valid ICU package"); that crash-looped into 259
  # coredumps 2026-10-05..18.
  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [ stdenv.cc.cc.lib zlib openssl icu ];

  programs.git = {
    enable = true;
    # Not merely the git-lfs package: this also writes the filter.lfs
    # clean/smudge/process entries into /etc/gitconfig, and without them a
    # clone of an LFS repo silently checks out pointer files instead of
    # content.
    lfs.enable = true;
    config = {
      receive.denyCurrentBranch = "updateInstead";
      init.defaultBranch = "main";
    };
  };

  # The AWS CLI's telemetry sqlite db lives under ~/.aws/cli/cache by default
  # — the one thing here that must never write anything under guest ~/.aws,
  # to hold the no-creds-at-rest invariant even though telemetry itself
  # carries no credentials.
  environment.variables.AWS_CLI_SESSION_ID_DISABLED = "true";
  # Cargo reads profile settings from the environment above both Cargo.toml
  # and .cargo/config.toml, so this caps dev/test DWARF for every project.
  environment.variables.CARGO_PROFILE_DEV_DEBUG = "line-tables-only";

  virtualisation.rosetta = {
    enable = true;
    mountTag = "vz-rosetta";
  };

  environment.systemPackages = with pkgs; [
    helix
    ripgrep
    fd
    bat
    jq
    yq-go
    tmux
    fzf
    zoxide
    eza
    btop
    (callPackage ./pkgs/far2l.nix { })
    delta
    gh
    forgejo-cli
    shellcheck
    shfmt
    cloc
    rune
    sqlite
    curl
    wget
    unzip
    devbox
    awscli2
  ];

  environment.etc."yolobox/version".text = version + "\n";

  nix.settings.experimental-features = [ "nix-command" "flakes" "fetch-closure" "ca-derivations" ];
  nix.settings.auto-optimise-store = true;
}
