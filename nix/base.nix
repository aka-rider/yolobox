{ config, pkgs, modulesPath, username, lib, ... }:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

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
  networking.hostName = "yolobox";
  users.mutableUsers = true;
  system.stateVersion = "25.11";

  # Takes over lima-init's existence-guarded useradd from first boot
  # (Gotcha 1) — including lima >=2.1.0's ".guest"-suffixed home
  # (lima-vm/lima#4578). That account is named after the host Mac user
  # (`username`, threaded in impurely — see flake.nix), not a fixed name:
  # there is no separate "yolobox account", this declares the same account
  # cidata already created so nix and interactive SSH sessions share one
  # $HOME. NixOS refuses isNormalUser below uid 1000 (lima's cidata UID
  # here is 501, matching the host's macOS UID), so this uses isSystemUser
  # instead and sets autoSubUidGidRange directly — that option isn't
  # actually gated on isNormalUser, only its *default* is — giving
  # rootless podman its /etc/subuid range declaratively.
  users.users.${username} = {
    isSystemUser = true;
    group = "users";
    uid = 501;
    home = "/home/${username}.guest";
    extraGroups = [ "wheel" ];
    shell = pkgs.zsh;
    autoSubUidGidRange = true;
  };

  services.openssh.enable = true;
  services.openssh.settings = {
    AcceptEnv = [
      "YOLOBOX_HERD"
      "HERDR_PANE_ID"
      "HERDR_SOCKET_PATH"
      "AWS_ACCESS_KEY_ID"
      "AWS_SECRET_ACCESS_KEY"
      "AWS_SESSION_TOKEN"
      "AWS_CREDENTIAL_EXPIRATION"
    ];
    StreamLocalBindUnlink = "yes";
  };

  # Forwarded herdr sockets (see cmd_enter in yo) land under /run, not
  # $HOME: a unix socket anywhere under a Nix source root makes evaluation of
  # that root fail with "file ... has an unsupported type", and /run is tmpfs
  # so an orphaned socket dies at reboot instead of accumulating. This is safe
  # ahead of any ssh connection because systemd-tmpfiles-setup.service runs
  # Before=sysinit.target, while sshd only arrives with multi-user.target.
  systemd.tmpfiles.rules = [ "d /run/yolobox 0700 ${username} users -" ];

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;

  # Rendered to /etc/direnv/direnv.toml, which is where the module's
  # DIRENV_CONFIG=/etc/direnv makes direnv look — an XDG ~/.config file is
  # never read. Agents cannot answer a `direnv allow` prompt, so every
  # .envrc under ~/wrk is auto-trusted; the VM itself is the blast-radius
  # boundary.
  programs.direnv = {
    enable = true;
    settings.whitelist.prefix = [ "${config.users.users.${username}.home}/wrk" ];
  };

  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [ stdenv.cc.cc.lib zlib openssl ];

  programs.git = {
    enable = true;
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
    delta
    gh
    shellcheck
    shfmt
    sqlite
    curl
    wget
    unzip
    devbox
    awscli2
  ];

  nix.settings.experimental-features = [ "nix-command" "flakes" "fetch-closure" "ca-derivations" ];
}
