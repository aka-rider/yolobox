{ config, lib, pkgs, modulesPath, ... }:
let
  homeDir = config.users.users.xiii.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
in
{
  # Boot/fs block below is verbatim from nixos-lima-config-sample's
  # nixos-lima-config.nix — it must match the shipped v0.2.1 image exactly
  # or the first `nixos-rebuild boot` bricks the VM (vz has no snapshots).
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  boot.loader.grub = {
    device = "nodev";
    efiSupport = true;
    efiInstallAsRemovable = true;
  };
  fileSystems."/boot" = {
    device = lib.mkForce "/dev/vda1"; # /dev/disk/by-label/ESP
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    autoResize = true;
    fsType = "ext4";
    options = [ "noatime" "nodiratime" "discard" ];
  };
  boot.kernelPackages = pkgs.linuxPackages_latest;

  services.lima.enable = true;
  networking.hostName = "yolobox";
  users.mutableUsers = true;
  system.stateVersion = "25.11";

  # Matches what lima-init's existence-guarded useradd already created on
  # first boot (Gotcha 1) — including lima >=2.1.0's ".guest"-suffixed home
  # (lima-vm/lima#4578). NixOS refuses isNormalUser below uid 1000
  # (lima's cidata UID here is 501, matching the host's macOS UID), so this
  # uses isSystemUser instead and sets autoSubUidGidRange directly — that
  # option isn't actually gated on isNormalUser, only its *default* is —
  # giving rootless podman its /etc/subuid range declaratively.
  users.users.xiii = {
    isSystemUser = true;
    group = "users";
    uid = 501;
    home = "/home/xiii.guest";
    extraGroups = [ "wheel" ];
    shell = pkgs.zsh;
    autoSubUidGidRange = true;
  };

  services.openssh.enable = true;
  services.openssh.settings = {
    AcceptEnv = [ "YOLOBOX_HERD" "HERDR_PANE_ID" "HERDR_SOCKET_PATH" ];
    StreamLocalBindUnlink = "yes";
  };

  # Forwarded herdr sockets (see cmd_enter in yolobox2) land under /run, not
  # $HOME: a unix socket anywhere under a Nix source root makes evaluation of
  # that root fail with "file ... has an unsupported type", and /run is tmpfs
  # so an orphaned socket dies at reboot instead of accumulating. This is safe
  # ahead of any ssh connection because systemd-tmpfiles-setup.service runs
  # Before=sysinit.target, while sshd only arrives with multi-user.target.
  systemd.tmpfiles.rules = [ "d /run/yolobox 0700 xiii users -" ]
    ++ homeTmpfiles {
      home = homeDir;
      dirUser = "xiii";
      dirs = [ ".config" ".config/direnv" ];
      rule = "L+";
      path = ".config/direnv/direnv.toml";
      argument = "/etc/yolobox/direnv/direnv.toml";
    };

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;
  programs.direnv.enable = true;

  # Agents cannot answer a `direnv allow` prompt, so every .envrc under ~/wrk
  # is auto-trusted — the VM itself is the blast-radius boundary.
  environment.etc."yolobox/direnv/direnv.toml".text = ''
    [whitelist]
    prefix = ["/home/xiii.guest/wrk"]
  '';

  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [ stdenv.cc.cc.lib zlib openssl ];

  programs.git = {
    enable = true;
    config = {
      receive.denyCurrentBranch = "updateInstead";
      init.defaultBranch = "main";
    };
  };

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
  ];

  nix.settings.experimental-features = [ "nix-command" "flakes" "fetch-closure" "ca-derivations" ];
}
