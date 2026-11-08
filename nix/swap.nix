{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.swapSizeMiB;
  # What /var must keep after the swapfile grows: a full root disk breaks name
  # resolution first (dhcpcd cannot write resolv.conf) and everything else after.
  reserveMiB = 1024;
  # NixOS's mkswap-var-swapfile unit recreates the file whenever its size
  # differs from `cfg`, on every boot, so an oversize value would fill the
  # disk at each boot. This runs first and refuses the growth that would
  # leave /var under the reserve. The old file is truncated before the new
  # one is written, so the growth is the difference in size.
  hasRoom = pkgs.writeShellScript "swapfile-has-room" ''
    set -eu
    current=$(( $(${pkgs.coreutils}/bin/stat -c %s /var/swapfile 2>/dev/null || echo 0) / 1024 / 1024 ))
    avail=$(${pkgs.coreutils}/bin/df --output=avail --block-size=1M /var | ${pkgs.coreutils}/bin/tail -n 1)
    need=$(( ${toString cfg} - current + ${toString reserveMiB} ))
    if [ "$current" -ne ${toString cfg} ] && [ "$avail" -lt "$need" ]; then
      echo "refusing to grow /var/swapfile to ${toString cfg} MiB: /var has $avail MiB free, the growth needs $need MiB with a ${toString reserveMiB} MiB reserve; lower yolobox.swapSizeMiB in /etc/yolobox/local.nix" >&2
      exit 1
    fi
  '';
in
{
  options.yolobox.swapSizeMiB = lib.mkOption {
    type = lib.types.ints.unsigned;
    default = 16384;
    description = ''
      Size of /var/swapfile in MiB; 0 means no swap. A box on a small disk
      sets it in /etc/yolobox/local.nix, because the swapfile is allocated
      in full on the root disk. The default is 16 GiB so that reclaim has
      somewhere to go before the kernel's OOM killer takes a user
      manager and every rootless container with it.
    '';
  };

  config = lib.mkMerge [
    (lib.mkIf (cfg > 0) {
      swapDevices = [ { device = "/var/swapfile"; size = cfg; } ];
      systemd.services.mkswap-var-swapfile.serviceConfig.ExecStartPre = "${hasRoom}";
    })
    # With 0 the unit above no longer exists, and the old file would stay on
    # the disk, unused, until deleted by hand.
    (lib.mkIf (cfg == 0) {
      systemd.tmpfiles.rules = [ "r /var/swapfile" ];
    })
  ];
}
