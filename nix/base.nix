{ config, pkgs, modulesPath, username, lib, version, hostname, flakeRef, yolobox, facts, ... }:
let
  opProxy = { name, listen, target }: {
    systemd.sockets.${name} = {
      description = "Agent-owned socket proxied to the operator's ${name} forward";
      # Wanted by no target: yo starts it once the forward it proxies exists.
      socketConfig = {
        ListenStream = listen;
        SocketUser = facts.agent_user;
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
        # The proxy runs as the operator, whose processes the agent cannot
        # ptrace, but a bug in proxyd itself would run as the operator: it
        # gets only what copying bytes between two unix sockets needs.
        # ProtectHome still allows the connect(): a unix connect needs write
        # on the socket inode, not on the mount.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateNetwork = true;
        RestrictAddressFamilies = "AF_UNIX";
        SystemCallFilter = "@system-service";
        CapabilityBoundingSet = "";
        ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd ${target}";
      };
    };
  };

  opProxies = map opProxy [
    { name = facts.op_agent_proxy_unit; listen = facts.agent_auth_sock; target = facts.op_guest_sock; }
    { name = facts.aws_broker_proxy_unit; listen = facts.agent_aws_broker_sock; target = facts.aws_broker_guest_sock; }
  ];
  # Where the host's forwards land, the operator's alone.
  opDir = builtins.dirOf facts.op_guest_sock;
in
{
  # Lets someone drop a package or setting into the running box without a
  # checkout or push: the guest carries no copy of this repo to edit.
  # Set to 1, the nix_env_skip_local_nix variable leaves it out, so a flake
  # check run inside a box evaluates the box CI evaluates.
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ] ++ lib.optional
    (builtins.getEnv facts.nix_env_skip_local_nix != "1" && builtins.pathExists /etc/yolobox/local.nix)
    /etc/yolobox/local.nix
    ++ opProxies;

  # /boot is the ESP itself, as the shipped nixos-lima image mounts it. It is
  # 249 MiB, a kernel set is ~92 MiB beside ~14 MiB of GRUB, and GRUB copies
  # the incoming kernel before it prunes: only one retained generation fits.
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
    device = "/dev/mapper/${facts.luks_mapper}";
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
  # nixos-lima's lima-init.nix sets kernel.unprivileged_userns_clone, a knob
  # only the removed -hardened kernels had; mainline has none, so systemd-sysctl
  # warned about it on every boot. User namespaces stay on through
  # user.max_user_namespaces.
  boot.kernel.sysctl."kernel.unprivileged_userns_clone" = lib.mkForce null;

  # A crash-looping language server (marksman, missing ICU) filled
  # /var/lib/systemd/coredump with dumps nobody read; systemd's default cap
  # is 10% of the filesystem, far too much for a box disk. `yo gc --yes`
  # empties it.
  systemd.coredump.settings.Coredump.MaxUse = "1G";

  # journald's default cap is 10% of the disk, at most 4 GiB, so nothing
  # bounded a flood below that. This is the backstop for every emitter; the
  # fix for container output is in nix/podman.nix.
  services.journald.settings.Journal.SystemMaxUse = "512M";

  services.lima.enable = true;
  # A switch must not restart lima-guestagent: it breaks every port Lima
  # forwards until the host agent restarts. systemd propagates a restart of
  # lima-init to it through Requires=, so both are pinned.
  systemd.services.lima-guestagent.restartIfChanged = false;
  systemd.services.lima-init.restartIfChanged = lib.mkForce false;
  # The unit runs the binary Lima ships on its cidata disk; a libvirt box's
  # cidata has none, so there it is skipped. The gate is the binary, never the host OS.
  systemd.services.lima-guestagent.unitConfig.ConditionPathExists = "/mnt/lima-cidata/lima-guestagent";
  # nixos-lima links /etc/environment to the cidata disk, which it mounts root
  # 0700, so every user manager failed to read it ("Failed to chase ...
  # Permission denied"). It is always empty here.
  environment.etc.environment.enable = false;

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
  # (`username`, threaded in impurely from YOLOBOX_USERNAME), not a fixed name:
  # there is no separate "yolobox account", this declares the same account
  # cidata already created so nix and interactive SSH sessions share one
  # $HOME. NixOS refuses isNormalUser below uid 1000 (lima's cidata UID
  # here is 501, pinned by user.uid in lima/yolobox.yaml), so this uses isSystemUser
  # instead. It keeps wheel and runs no agent; rootless podman's
  # autoSubUidGidRange lives on the agent account below instead.
  users.users.${username} = {
    isSystemUser = true;
    group = "users";
    uid = facts.operator_uid;
    home = "/home/${username}${facts.operator_home_suffix}";
    extraGroups = [ "wheel" ];
    shell = pkgs.zsh;
  };

  # The agent's account. uid must not be 501: a second account sharing
  # lima's uid splits state silently —
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
  users.users.${facts.agent_user} = {
    isNormalUser = true;
    uid = facts.agent_uid;
    group = "users";
    home = facts.agent_home;
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
  # another !. A missing shadow entry fails the activation: the `false`
  # trips the runner's ERR trap, which marks this snippet failed and makes
  # the activation exit nonzero after the later snippets have run. An
  # `exit` here would skip them instead.
  system.activationScripts.lockRootAndAgent = {
    deps = [ "users" ];
    text = ''
      for account in root ${facts.agent_user}; do
        if ! entry="$(${pkgs.getent}/bin/getent shadow "$account")"; then
          echo "lockRootAndAgent: no shadow entry for $account" >&2
          false
          continue
        fi
        field="''${entry#*:}"; field="''${field%%:*}"
        case "$field" in
          '!'*|'*'*) ;;
          *) ${pkgs.shadow}/bin/passwd -l "$account" ;;
        esac
      done
    '';
  };

  # username reaches shell scripts, sshd config and tmpfiles rules below, and
  # must not be able to impersonate the agent; flake.nix already refuses the
  # agent's and root's names.
  assertions = [
    {
      assertion = builtins.match facts.operator_account_re username != null;
      message = "YOLOBOX_USERNAME \"${username}\" is not a plain lowercase unix name (${facts.operator_account_re}).";
    }
  ];

  # github.com's published host keys, in /etc/ssh/ssh_known_hosts for both
  # accounts, so the first clone needs no trust-on-first-use prompt
  # (verify.yml checks their fingerprints against GitHub's published ones).
  programs.ssh.knownHosts = {
    github-ed25519 = {
      hostNames = [ "github.com" ];
      publicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl";
    };
    github-ecdsa = {
      hostNames = [ "github.com" ];
      publicKey = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=";
    };
    github-rsa = {
      hostNames = [ "github.com" ];
      publicKey = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=";
    };
  };

  services.openssh.enable = true;
  # Lima's hostagent waits for, and forwards to, the guest's ssh port on
  # 192.168.5.15 (enp0s1, lima's NAT network, unreachable from the LAN), so that
  # is the only port open there. tailscale0 is trusted whole (nix/tailscale.nix).
  services.openssh.openFirewall = false;
  networking.firewall.interfaces.${facts.guest_nic}.allowedTCPPorts = [ facts.guest_ssh_port ];
  services.openssh.settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
    # root is locked and AllowUsers below never lists it; this says so in
    # the one place sshd reads first. The initrd's sshd, where root is the
    # unlock login, renders its own config (nix/luks.nix).
    PermitRootLogin = "no";
    # yo asks its master for both reverse forwards on every session command,
    # and a socket left by a master that died must not refuse the next bind.
    StreamLocalBindUnlink = true;
  };
  # `yo cp` uploads with scp's sftp mode, and an upload rests on no
  # guest shell running for a transfer. An external sftp-server is spawned
  # through the user's login shell (`$SHELL -c`), which sources the agent's
  # ~/.zshenv first; the in-process server runs no shell at all.
  services.openssh.sftpServerExecutable = "internal-sftp";
  # Both accounts log in only from the gateway, where every yo and herdr
  # session arrives, and which the agent cannot spoof off enp0s1. The agent
  # holds the forwarded 1Password agent, which serves the operator's key, and
  # on a Linux host that one key opens every box of the host: from anywhere
  # else (the tailnet, loopback) neither account is a way in. It holds on every
  # box, whatever the host OS.
  services.openssh.settings.AllowUsers = [ "${username}@${facts.gateway}" "${facts.agent_user}@${facts.gateway}" ];
  # Every host connection arrives from the gateway, so a penalty on it locks
  # the host out; yo reads the banner mark so it never offers stage 2 a root login.
  services.openssh.settings.PerSourcePenaltyExemptList = facts.gateway;
  services.openssh.settings.VersionAddendum = facts.stage2_ssh_banner_mark;
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 ! -i ${facts.guest_nic} -s ${facts.gateway} -j DROP
  '';
  networking.firewall.extraStopCommands = ''
    if iptables -C nixos-fw ! -i ${facts.guest_nic} -s ${facts.gateway} -j DROP; then
      iptables -D nixos-fw ! -i ${facts.guest_nic} -s ${facts.gateway} -j DROP
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
  environment.etc.${lib.removePrefix "/etc/" facts.agent_keys_command} = {
    mode = "0555";
    text = ''
      #!${pkgs.runtimeShell}
      exec ${pkgs.coreutils}/bin/cat ${lib.escapeShellArg "/etc/ssh/authorized_keys.d/${username}"}
    '';
  };
  # `Match all` closes the block: a Match block runs to the end of the file,
  # so a later extraConfig line would otherwise apply to the agent only.
  services.openssh.extraConfig = ''
    Match User ${facts.agent_user}
      AuthorizedKeysFile none
      AuthorizedKeysCommand ${facts.agent_keys_command}
      AuthorizedKeysCommandUser root
    Match all
  '';

  systemd.tmpfiles.rules = [
    "d ${opDir} 0700 ${username} users -"
  ];

  environment.localBinInPath = true;

  environment.extraInit = ''
    if [ "$(id -un)" = ${lib.escapeShellArg facts.agent_user} ]; then
      ${lib.concatStringsSep "\n" (lib.mapAttrsToList
        (name: value: "export ${name}=${lib.escapeShellArg value}")
        yolobox.agentEnv)}
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
      # git refuses it when the worktree or index differs from HEAD.
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
  # rosetta share; not `nofail`, which races systemd-binfmt.
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

  environment.etc.${lib.removePrefix "/etc/" facts.version_stamp}.text = version + "\n";
  # The exact tree the box was built from, so `yolobox-guest rebuild` builds
  # it again instead of guessing.
  environment.etc.${lib.removePrefix "/etc/" facts.flake_ref_stamp}.text = flakeRef + "\n";
  # Every fact the box's shell scripts read, one KEY=value line each, for
  # `. /etc/yolobox/facts.sh`. The modules contribute them through
  # yolobox.facts; this is the one place that renders them.
  environment.etc."yolobox/facts.sh".text =
    lib.concatStrings (lib.mapAttrsToList (key: value: "${key}=${lib.escapeShellArg value}\n") config.yolobox.facts);

  # The box is built from a flake ref with three variables set, which a bare
  # `nixos-rebuild switch` knows none of. Without a nixos-config it fails with
  # "file 'nixos-config' was not found", so this one says what to run instead.
  nix.nixPath = [
    "nixpkgs=flake:nixpkgs"
    "nixos-config=${pkgs.writeText "nixos-config.nix" ''
      throw "this box is built from a flake ref: run `yolobox-guest rebuild switch` (or `boot`) instead of nixos-rebuild"
    ''}"
  ];

  nix.settings.experimental-features = [ "nix-command" "flakes" "fetch-closure" "ca-derivations" ];
  nix.settings.auto-optimise-store = true;
}
