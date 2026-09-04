{ config, pkgs, modulesPath, username, agentUser, lib, version, ... }:
{
  # Lets someone drop a package or setting into the running box without a
  # checkout or push: the guest no longer carries a copy of this repo, so
  # the old "edit nix/base.nix in the VM and rebuild" workflow needs a
  # replacement.
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ] ++ lib.optional (builtins.pathExists /etc/yolobox/local.nix) /etc/yolobox/local.nix;

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

  swapDevices = [{ device = "/var/swapfile"; size = 16384; }];

  services.lima.enable = true;
  # nixos-lima declares lima-init and lima-guestagent as ordinary NixOS
  # units, so a nixpkgs bump alone rehashes the store paths their
  # Environment= and ExecStart= embed and switch-to-configuration reads that
  # as "restart". Restarting the guestagent is not an ordinary bounce: lima
  # 2.2.0 serves the event stream and the TCP Tunnel RPC from one
  # grpc.ClientConn, and on reconnect it replaces that conn but never
  # refreshes the dialer an existing listener already captured, so every
  # forwarded port accepts and then resets until the host agent itself is
  # restarted. Both units must be pinned, not just the guestagent:
  # stopIfChanged defaults to true, and a stopped lima-init drags the
  # guestagent down through its Requires=. mkForce only on lima-init,
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
  # "The guest account mirrors the host") — passwd lookups resolve a uid to
  # whichever entry comes first, so two accounts at 501 are indistinguishable
  # to whoami/SUDO_USER while modules silently write state into the wrong
  # home. It is deliberately not lima's cidata account either, because
  # lima-init runs `usermod -a -G wheel $LIMA_CIDATA_USER` unconditionally on
  # every boot, after activation — removing wheel from that account here
  # would not stick. systemd-journal is required, not cosmetic: xvfb,
  # openbox and t3 are system units, so their logs go to the system journal,
  # whose ACL grants read only to wheel and adm (other::---) — without this
  # group `journalctl -u t3` silently narrows to the agent's own messages.
  users.users.${agentUser} = {
    isNormalUser = true;
    uid = 1000;
    group = "users";
    home = "/home/${agentUser}";
    shell = pkgs.zsh;
    autoSubUidGidRange = true;
    extraGroups = [ "systemd-journal" ];
  };

  services.openssh.enable = true;
  services.openssh.settings = {
    AcceptEnv = [
      "YOLOBOX_HERD"
      "HERDR_PANE_ID"
      "HERDR_SOCKET_PATH"
      "AWS_CONTAINER_CREDENTIALS_FULL_URI"
      "AWS_CONTAINER_AUTHORIZATION_TOKEN"
      "AWS_REGION"
    ];
    StreamLocalBindUnlink = "yes";
    PasswordAuthentication = false;
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
      exec ${pkgs.coreutils}/bin/cat /etc/ssh/authorized_keys.d/${username}
    '';
  };
  services.openssh.extraConfig = ''
    Match User ${agentUser}
      AuthorizedKeysFile none
      AuthorizedKeysCommand /etc/ssh/agent-authorized-keys
      AuthorizedKeysCommandUser root
  '';

  # Forwarded herdr sockets (see cmd_enter in yo) land under /run, not
  # $HOME: a unix socket anywhere under a Nix source root makes evaluation of
  # that root fail with "file ... has an unsupported type", and /run is tmpfs
  # so an orphaned socket dies at reboot instead of accumulating. This is safe
  # ahead of any ssh connection because systemd-tmpfiles-setup.service runs
  # Before=sysinit.target, while sshd only arrives with multi-user.target.
  # Owned by the agent, not the operator: sshd binds the -R herd forward as
  # the agent, and an operator-owned 0700 directory would break every
  # `yo enter`.
  #
  # The two claude rules are the boot-and-switch backstop for the user path
  # unit in nix/harnesses.nix: a `claude update` run while no user manager of
  # the agent's was up leaves ~/.local/bin/claude behind, and because the
  # agent's dotfiles prepend that directory it would shadow the wrapped
  # claude — and its herd hooks — in every later shell. `R` on the install
  # root, not just the launcher, because the versions directory under it is
  # what the launcher points into and is ~216 MB per version with nothing to
  # prune it.
  systemd.tmpfiles.rules = [
    "d /run/yolobox 0700 ${agentUser} users -"
    "r ${config.users.users.${agentUser}.home}/.local/bin/claude"
    "R ${config.users.users.${agentUser}.home}/.local/share/claude"
  ];

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;

  # Rendered to /etc/direnv/direnv.toml, which is where the module's
  # DIRENV_CONFIG=/etc/direnv makes direnv look — an XDG ~/.config file is
  # never read.
  # The whitelist covers only the agent's home: an .envrc is arbitrary code
  # the agent writes, and auto-trusting it in the operator's shell would
  # hand over the operator's sudo on the first cd.
  programs.direnv = {
    enable = true;
    settings.whitelist.prefix = [ config.users.users.${agentUser}.home ];
  };

  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [ stdenv.cc.cc.lib zlib openssl ];

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
    delta
    gh
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
}
