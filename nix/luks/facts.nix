# The literals the LUKS setup spells in more than one place, defined once.
# nix/luks.nix and nix/luks/apps.nix import this file. The Python side and
# nix/base.nix state the same facts, and tests/test_consistency.py ties them:
# device, mapper and sshLocalPort to yo.LUKS_DEVICE, yo.LUKS_MAPPER and
# yo.SSH_LOCAL_PORT (the last also to ssh.localPort in lima/yolobox.yaml), and
# mapper to the /dev/mapper/<mapper> root in nix/base.nix.
{
  # The partition that holds the root filesystem.
  device = "/dev/vda2";
  # Written to the LUKS header when the migration finishes; seeing it means
  # the disk is done and nothing is asked on later boots.
  label = "yolobox-luks";
  # The name of the opened mapping: /dev/mapper/<mapper>.
  mapper = "root";
  # Where lima's hostagent listens on the Mac for the initrd sshd.
  sshLocalPort = 60022;
}
