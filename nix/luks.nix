# The root partition is LUKS2. `yo up` derives the passphrase from a
# 1Password-held SSH key and feeds it to an sshd inside the initrd; the seed
# image's first boot of this generation encrypts the plain ext4 root in place.
# The scripts live in nix/luks/ and their definitions in nix/luks/apps.nix.
{ config, pkgs, lib, utils, facts, ... }:
let
  device = facts.luks_device;
  # growpart takes the disk and the partition number apart.
  growpartArgs = builtins.match "(.*[^0-9])([0-9]+)" device;
  label = facts.luks_label;
  mapper = facts.luks_mapper;
  cryptsetupUnit = "systemd-cryptsetup@${mapper}.service";
  cidataDevice = "/dev/disk/by-label/cidata";
  cidataUnit = "${utils.escapeSystemdPath cidataDevice}.device";
  deviceUnit = "${utils.escapeSystemdPath device}.device";
  apps = import ./luks/apps.nix { inherit pkgs facts; systemd = config.systemd.package; };
in
{
  # The unlock relies on stage 1 being systemd: password agents, unit ordering
  # and the encrypt unit all assume it. nixpkgs removed the scripted initrd
  # and with it `boot.initrd.systemd.enable`: systemd stage 1 is the only one.

  # The cidata ISO is mounted in the initrd to read lima's key.
  boot.initrd.kernelModules = [ "isofs" ];

  # dm-crypt in the initrd, and /etc/crypttab for systemd-cryptsetup@<mapper>.service.
  # Discards pass through so the host's sparse disk file stays sparse. tries=1:
  # a wrong passphrase fails the unit at once instead of asking again, which is
  # how yolobox-unlock learns the verdict.
  boot.initrd.luks.devices.${mapper} = {
    inherit device;
    allowDiscards = true;
    crypttabExtraOpts = [ "tries=1" ];
  };

  # sshd in the initrd is the only way to deliver the passphrase: the vz serial
  # console is output only.
  boot.initrd.network.enable = true;
  # The initrd resolves no names. Its resolved, on by default with initrd
  # networking, sends LLMNR multicast that passt cannot carry and logs once per
  # packet on a Linux host.
  boot.initrd.services.resolved.enable = false;
  boot.initrd.network.ssh = {
    enable = true;
    # The host key is generated per boot (preStart below), so no private key
    # sits on the unencrypted ESP.
    ignoreEmptyHostKeys = true;
    # The module asserts a non-empty list; sshd ignores a # line. The real key
    # is read from cidata at boot.
    authorizedKeys = [ "# lima's key is read from cidata at boot" ];
    # Lima's readiness loop fails as an invalid user every 3 s from the same
    # source address as `yo`; OpenSSH 10's per-source penalties would block
    # that address for up to 600 s. The banner word is how yo knows this sshd
    # is the initrd's, the only one it offers root and the passphrase. Global
    # keywords must precede `Match`, or they belong to the block and sshd
    # refuses the whole config.
    extraConfig = ''
      PerSourcePenalties no
      VersionAddendum ${facts.initrd_ssh_banner_mark}
      Match User root
        AuthorizedKeysFile /etc/ssh/yolobox_authorized_keys
    '';
  };

  boot.initrd.systemd.extraBin = {
    cryptsetup = "${pkgs.cryptsetup}/bin/cryptsetup";
    ssh-keygen = "${pkgs.openssh}/bin/ssh-keygen";
    ${facts.initrd_unlock_helper} = lib.getExe apps.unlock;
  };

  # The scripts and every tool they run (apps.inputs); a directory is copied
  # whole. systemd-reply-password is not in the initrd's systemd by default.
  boot.initrd.systemd.storePaths = [
    apps.keys
    apps.unlock
    apps.encrypt
    "${config.systemd.package}/lib/systemd/systemd-reply-password"
  ] ++ map (p: "${p}/bin") apps.inputs;

  boot.initrd.systemd.services.sshd = {
    # lima's key comes from the cidata ISO, so it must exist first.
    wants = [ cidataUnit ];
    after = [ cidataUnit ];
    preStart = ''
      ${lib.getExe apps.keys} /etc/ssh/yolobox_authorized_keys
      [ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key
    '';
  };

  boot.initrd.systemd.services.${facts.luks_encrypt_unit} = {
    description = "Encrypt ${device} in place before it is opened";
    # requiredBy: a failed encryption stops the cryptsetup unit with a
    # dependency failure, so it is never misread as a wrong passphrase.
    requiredBy = [ cryptsetupUnit ];
    wants = [ "cryptsetup-pre.target" ];
    requires = [ deviceUnit ];
    after = [ deviceUnit ];
    before = [ cryptsetupUnit "cryptsetup-pre.target" "shutdown.target" ];
    conflicts = [ "shutdown.target" ];
    unitConfig = {
      DefaultDependencies = false;
      # With RemainAfterExit: the switch-root isolate neither restarts the
      # script nor stops the unit, which through Requires= would stop the
      # cryptsetup unit and detach the root mapping under /sysroot.
      IgnoreOnIsolate = true;
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # It waits for the passphrase indefinitely, then encrypts for hours.
      TimeoutStartSec = "infinity";
    };
    script = "exec ${lib.getExe apps.encrypt} ${device} ${label} ${mapper}";
  };

  # cryptsetup resize and luksDump need the CLI in stage 2; only the
  # systemd-cryptsetup helper is in the initrd by default.
  environment.systemPackages = [ pkgs.cryptsetup ];

  # Upstream derives the partition from fileSystems."/".device, which is
  # /dev/mapper/root now and resolves to /dev/dm-0, so the partition is named.
  systemd.services.growpart.script = lib.mkForce ''"${pkgs.cloud-utils.guest}/bin/growpart" ${lib.escapeShellArgs growpartArgs}'';
}
