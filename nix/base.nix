{ config, pkgs, modulesPath, username, agentUser, lib, version, hostname, flakeRef, ... }:
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

  opProxies = map opProxy [
    { name = "yolobox-op-agent"; listen = agentEnv.env.SSH_AUTH_SOCK; target = "agent.sock"; }
    { name = "yolobox-aws-broker"; listen = agentEnv.awsBrokerSock; target = "aws-broker.sock"; }
  ];
in
{
  # Lets someone drop a package or setting into the running box without a
  # checkout or push: the guest carries no copy of this repo to edit.
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
    device = "/dev/mapper/root";
    fsType = "ext4";
    options = [ "noatime" "nodiratime" "discard" ];
  };
  # Growing the root disk is three steps. growpart below grows vda2 into
  # whatever the disk has become, on every boot, and SuccessExitStatus "0 1"
  # makes the already-grown case a no-op. The LUKS mapping and the ext4
  # filesystem inside it are grown by `yo disk-grow`, which holds the
  # passphrase `cryptsetup resize` needs; autoResize is off because
  # systemd-growfs cannot resize a keyring-keyed LUKS2 mapping. Without
  # growpart the bump is silent: the backing file gets bigger, the guest boots
  # clean, vda2 stays where it was and df is unchanged. Only vda2 moves —
  # vda1 ends at the sector vda2 starts on, so the ESP above is unaffected.
  boot.growPartition = true;
  boot.kernelPackages = pkgs.linuxPackages_latest;
  # Same-uid ptrace (py-spy, gdb, strace -p). The uid split, not Yama, is the
  # boundary; see nix/base.md#same-uid-ptrace.
  boot.kernel.sysctl."kernel.yama.ptrace_scope" = 0;

  # nixos-lima's lima-init.nix sets kernel.unprivileged_userns_clone, a knob
  # only the removed -hardened kernels had; mainline has none, so systemd-sysctl
  # warned about it on every boot. User namespaces stay on through
  # user.max_user_namespaces. See nix/base.md#sysctl-keys-the-kernel-lacks.
  boot.kernel.sysctl."kernel.unprivileged_userns_clone" = lib.mkForce null;

  # A crash-looping language server (marksman, missing ICU) filled
  # /var/lib/systemd/coredump with dumps nobody read; systemd's default cap
  # is 10% of the filesystem, far too much for a box disk. `yo gc --yes`
  # empties it.
  systemd.coredump.settings.Coredump.MaxUse = "1G";

  # journald's default cap is 10% of the disk, at most 4 GiB, so nothing
  # bounded a flood below that. This is the backstop for every emitter; the
  # fix for container output is in nix/podman.nix (story: nix/podman.md).
  services.journald.settings.Journal.SystemMaxUse = "512M";

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
  # The unit runs the binary Lima ships on its cidata disk; a libvirt box's
  # cidata has none, so there it is skipped (nix/base.md, "No host OS ...").
  systemd.services.lima-guestagent.unitConfig.ConditionPathExists = "/mnt/lima-cidata/lima-guestagent";

  # Upstream's user@.service ships OOMScoreAdjust=100, making the per-user
  # manager a preferred kernel-OOM victim over every system service; killing
  # it takes down every rootless podman container at once.
  systemd.services."user@" = {
    overrideStrategy = "asDropin";
    serviceConfig.OOMScoreAdjust = -500;
  };

  networking.hostName = hostname;
  # nixos-lima turns NAT on, and nixpkgs' nat module turns IPv4 forwarding on
  # with it. Nothing here routes for others: rootless podman uses pasta and
  # tailscale is only a client. Forwarding would let the host side route
  # through the guest into the tailnet.
  networking.nat.enable = lib.mkForce false;
  users.mutableUsers = true;
  system.stateVersion = "25.11";

  # This is the operator's account — the human who drives the Mac, not any
  # agent. Takes over lima-init's existence-guarded useradd from first boot, including lima >=2.1.0's ".guest"-suffixed home
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
  # lima's uid splits state silently (see docs/accounts.md#two-accounts) —
  # passwd lookups resolve a uid to
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
  # (enp0s1, lima's NAT network, unreachable from the LAN), so 22 is the only
  # port open there. tailscale0 is trusted whole (nix/tailscale.nix).
  services.openssh.openFirewall = false;
  networking.firewall.interfaces.enp0s1.allowedTCPPorts = [ 22 ];
  services.openssh.settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
    PermitRootLogin = "prohibit-password";
  };
  # The operator logs in only from the gateway, which the agent cannot spoof
  # off enp0s1, so its 1Password agent is no way in (nix/base.md, "No host OS ...").
  services.openssh.settings.AllowUsers = [ "${username}@192.168.5.2" agentUser ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 ! -i enp0s1 -s 192.168.5.2 -j DROP
  '';
  networking.firewall.extraStopCommands = ''
    if iptables -C nixos-fw ! -i enp0s1 -s 192.168.5.2 -j DROP; then
      iptables -D nixos-fw ! -i enp0s1 -s 192.168.5.2 -j DROP
    fi
  '';
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
  # Drops other-execute from the setuid sudo wrapper, so the non-wheel agent
  # cannot even exec it. Only the operator, who is in wheel, runs sudo.
  security.sudo.execWheelOnly = true;
  programs.zsh.enable = true;

  # .NET binaries, such as the marksman a pi LSP extension the user installs
  # downloads, abort without ICU ("Couldn't find a valid ICU package"), and a
  # language server that aborts on start crash-loops into a pile of coredumps.
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
      # A push into a project checkout in the VM updates its worktree, and
      # git refuses it when the worktree or index differs from HEAD; see
      # nix/base.md#pushing-into-a-vm-checkout.
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

  # Only Lima/vz on Apple silicon runs an aarch64 guest, and it always has the
  # rosetta share; not `nofail`, which races systemd-binfmt (nix/base.md).
  virtualisation.rosetta = lib.mkIf pkgs.stdenv.hostPlatform.isAarch64 {
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
    traceroute
    delta
    gh
    shellcheck
    shfmt
    cloc
    sqlite
    curl
    wget
    dnsutils
    unzip
    devbox
    awscli2
  ];

  environment.etc."yolobox/version".text = version + "\n";
  # What built the box, so a rebuild from inside it reuses both instead of
  # guessing: the flake ref and the hostname. `yolobox-guest rebuild` reads
  # them (nix/guest/yolobox-guest.md).
  environment.etc."yolobox/flake-ref".text = flakeRef + "\n";
  environment.etc."yolobox/hostname".text = hostname + "\n";

  nix.settings.experimental-features = [ "nix-command" "flakes" "fetch-closure" "ca-derivations" ];
  nix.settings.auto-optimise-store = true;
}
