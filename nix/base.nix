{ config, pkgs, modulesPath, username, ... }:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  # The prebuilt nixos-lima qcow2 sizes its ESP with make-disk-image's
  # bootSize default, 249 MiB, and nothing in this repo can grow it: vda1
  # ends where vda2 begins, and the disk is fully allocated. A kernel set
  # here is ~91 MiB, so the ESP holds two of them — and
  # linuxPackages_latest ships a new one every few weeks. Once it fills,
  # the failure lands late and looks like a build problem it isn't: the
  # closure builds fine, the system profile advances, and activation then
  # dies installing the bootloader with "No space left on device",
  # leaving the box booted on the old generation with a profile that
  # claims otherwise. So /boot lives on the 99 GiB ext4 root (this GRUB
  # reads ext4) and the ESP is demoted to /boot/efi, where it carries
  # nothing but BOOTAA64.EFI.
  boot.loader.grub = {
    device = "nodev";
    efiSupport = true;
    efiInstallAsRemovable = true;
    configurationLimit = 10;
  };
  boot.loader.efi.efiSysMountPoint = "/boot/efi";
  fileSystems."/boot/efi" = {
    device = "/dev/vda1"; # /dev/disk/by-label/ESP
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
    AcceptEnv = [ "YOLOBOX_HERD" "HERDR_PANE_ID" "HERDR_SOCKET_PATH" ];
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
