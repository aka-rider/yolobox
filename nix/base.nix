{ config, lib, pkgs, modulesPath, ... }:
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
  users.mutableUsers = true;
  system.stateVersion = "25.11";

  services.openssh.enable = true;
  services.openssh.settings = {
    AcceptEnv = [ "YOLOBOX_HERD" "HERDR_PANE_ID" "HERDR_SOCKET_PATH" ];
    StreamLocalBindUnlink = "yes";
  };

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;
  programs.direnv.enable = true;

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
  ];

  nix.settings.experimental-features = [ "nix-command" "flakes" ];
}
