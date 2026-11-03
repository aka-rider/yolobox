# The literals the LUKS setup spells in more than one place, defined once.
# nix/luks.nix and nix/luks/apps.nix import this file. The Python side and
# nix/base.nix state the same facts, and tests/test_consistency.py ties them:
# device and mapper to yo.LUKS_DEVICE and yo.LUKS_MAPPER, and mapper to the
# /dev/mapper/<mapper> root in nix/base.nix. The initrd sshd's Mac-side port is
# not here: it differs per instance, so no initrd script may print it.
{
  # The partition that holds the root filesystem.
  device = "/dev/vda2";
  # Written to the LUKS header when the migration finishes; seeing it means
  # the disk is done and nothing is asked on later boots.
  label = "yolobox-luks";
  # The name of the opened mapping: /dev/mapper/<mapper>.
  mapper = "root";
}
