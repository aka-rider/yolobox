{ config, lib, ... }:
let
  cfg = config.yolobox.swapSizeMiB;
in
{
  options.yolobox.swapSizeMiB = lib.mkOption {
    type = lib.types.ints.unsigned;
    default = 16384;
    description = ''
      Size of /var/swapfile in MiB; 0 means no swap. A box on a small disk
      sets it in /etc/yolobox/local.nix, because the swapfile is allocated
      in full on the root disk. Why the default is 16 GiB: nix/base.md,
      "Memory pressure".
    '';
  };

  config.swapDevices = lib.optional (cfg > 0) { device = "/var/swapfile"; size = cfg; };
}
