# The root partition is LUKS2. `yo up` derives the passphrase from a
# 1Password-held SSH key and feeds it to an sshd inside the initrd; the first
# boot of this generation also encrypts the plain ext4 root in place. The
# scripts live in nix/luks/ and their definitions in nix/luks/apps.nix.
{ config, pkgs, lib, utils, ... }:
let
  device = "/dev/vda2";
  label = "yolobox-luks";
  cidataDevice = "/dev/disk/by-label/cidata";
  cidataUnit = "${utils.escapeSystemdPath cidataDevice}.device";
  deviceUnit = "${utils.escapeSystemdPath device}.device";
  apps = import ./luks/apps.nix { inherit pkgs; systemd = config.systemd.package; };
in
{
  # The unlock relies on stage 1 being systemd: password agents, unit ordering
  # and the migration unit all assume it.
  boot.initrd.systemd.enable = true;

  # The cidata ISO is mounted in the initrd to read lima's key.
  boot.initrd.kernelModules = [ "isofs" ];

  # dm-crypt in the initrd, and /etc/crypttab for systemd-cryptsetup@root.
  # Discards pass through so the host's sparse disk file stays sparse.
  boot.initrd.luks.devices.root = {
    inherit device;
    allowDiscards = true;
  };

  # sshd in the initrd is the only way to deliver the passphrase: the vz serial
  # console is output only.
  boot.initrd.network.enable = true;
  boot.initrd.network.ssh = {
    enable = true;
    # The host key is generated per boot (preStart below), so no private key
    # sits on the unencrypted ESP.
    ignoreEmptyHostKeys = true;
    # The module asserts a non-empty list; sshd ignores a # line. The real key
    # is read from cidata at boot.
    authorizedKeys = [ "# lima's key is read from cidata at boot, see nix/luks.nix" ];
    # Lima's readiness loop fails as an invalid user every 3 s from the same
    # source address as `yo`; OpenSSH 10's per-source penalties would block
    # that address for up to 600 s. The global keyword must precede `Match`,
    # or it belongs to the block and sshd refuses the whole config.
    extraConfig = ''
      PerSourcePenalties no
      Match User root
        AuthorizedKeysFile /etc/ssh/yolobox_authorized_keys
    '';
  };

  boot.initrd.systemd.extraBin = {
    cryptsetup = "${pkgs.cryptsetup}/bin/cryptsetup";
    ssh-keygen = "${pkgs.openssh}/bin/ssh-keygen";
    yolobox-unlock = lib.getExe apps.unlock;
  };

  # The initrd builder follows ELF dependencies only, never a shell script's
  # references, so the scripts' tools are listed by hand (a directory is
  # copied whole). Every package in nix/luks/apps.nix's runtimeInputs is here
  # (TestLuksStorePathsCoverTheScripts). The exception is systemd: systemctl,
  # journalctl and systemd-ask-password come from the initrd's own systemd
  # (initrdBin), the same package as config.systemd.package.
  boot.initrd.systemd.storePaths = [
    apps.keys
    apps.unlock
    apps.migrate
    "${pkgs.coreutils}/bin"
    "${pkgs.gawk}/bin"
    "${pkgs.gnused}/bin"
    "${pkgs.gnugrep}/bin"
    "${pkgs.util-linux}/bin"
    "${pkgs.cryptsetup}/bin"
    "${pkgs.e2fsprogs}/bin"
    "${pkgs.inotify-tools}/bin"
    "${config.systemd.package}/lib/systemd/systemd-reply-password"
  ];

  boot.initrd.systemd.services.sshd = {
    # lima's key comes from the cidata ISO, so it must exist first.
    wants = [ cidataUnit ];
    after = [ cidataUnit ];
    preStart = ''
      ${lib.getExe apps.keys} /etc/ssh/yolobox_authorized_keys
      [ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key
    '';
  };

  boot.initrd.systemd.services.yolobox-luks-migrate = {
    description = "Encrypt or finish encrypting ${device} in place before it is opened";
    # requiredBy: a failed migration stops the cryptsetup unit with a
    # dependency failure, so it is never misread as a wrong passphrase.
    requiredBy = [ "systemd-cryptsetup@root.service" ];
    wants = [ "cryptsetup-pre.target" ];
    requires = [ deviceUnit ];
    after = [ deviceUnit ];
    before = [ "systemd-cryptsetup@root.service" "cryptsetup-pre.target" "shutdown.target" ];
    conflicts = [ "shutdown.target" ];
    unitConfig = {
      DefaultDependencies = false;
      # With RemainAfterExit this is the pair systemd's own cryptsetup
      # generator gives the cryptsetup unit. RemainAfterExit keeps the unit
      # active, so later start transactions (the switch-root isolate wants a
      # target that requires the cryptsetup unit) drop the migrate job as
      # redundant instead of running the script again mid-switch-root.
      # IgnoreOnIsolate keeps that isolate from stopping the active unit,
      # which through Requires= would stop the cryptsetup unit and detach the
      # root mapping under the mounted /sysroot.
      IgnoreOnIsolate = true;
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # It waits for the passphrase indefinitely, then encrypts for hours.
      TimeoutStartSec = "infinity";
    };
    script = "exec ${lib.getExe apps.migrate} ${device} ${label}";
  };

  # cryptsetup resize and luksDump need the CLI in stage 2; only the
  # systemd-cryptsetup helper is in the initrd by default.
  environment.systemPackages = [ pkgs.cryptsetup ];

  # Upstream derives the partition from fileSystems."/".device, which is
  # /dev/mapper/root now and resolves to /dev/dm-0. See
  # docs/disk-encryption.md#growing-an-encrypted-disk.
  systemd.services.growpart.script = lib.mkForce ''"${pkgs.cloud-utils.guest}/bin/growpart" /dev/vda 2'';
}
