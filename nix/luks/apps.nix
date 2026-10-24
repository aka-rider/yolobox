# The three initrd scripts as writeShellApplications, defined once: nix/luks.nix
# puts them in the initrd and flake.nix exports them as packages, so the
# rehearsal runs the very derivations the initrd carries.
{ pkgs, systemd }:
{
  keys = pkgs.writeShellApplication {
    name = "yolobox-initrd-authorized-keys";
    runtimeInputs = [ pkgs.coreutils pkgs.gawk pkgs.util-linux ];
    text = builtins.readFile ./yolobox-initrd-authorized-keys.sh;
  };
  unlock = pkgs.writeShellApplication {
    name = "yolobox-unlock";
    runtimeInputs = [ pkgs.coreutils pkgs.gnused pkgs.inotify-tools systemd ];
    runtimeEnv = {
      REPLY_PASSWORD = "${systemd}/lib/systemd/systemd-reply-password";
      LUKS_DEVICE = "/dev/vda2";
    };
    text = builtins.readFile ./yolobox-unlock.sh;
  };
  migrate = pkgs.writeShellApplication {
    name = "yolobox-luks-migrate";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gawk
      pkgs.gnugrep
      pkgs.util-linux
      pkgs.cryptsetup
      pkgs.e2fsprogs
      systemd
    ];
    text = builtins.readFile ./yolobox-luks-migrate.sh;
  };
}
