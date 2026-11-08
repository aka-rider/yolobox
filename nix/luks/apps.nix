# The three initrd scripts as writeShellApplications, defined once: nix/luks.nix
# puts them and their tools in the initrd.
{ pkgs, systemd, facts }:
let
  # The initrd builder follows ELF dependencies only, never a shell script's
  # references, so the scripts' tools are listed by hand, once, and luks.nix
  # copies every bin directory. systemd is the initrd's own (initrdBin).
  inputs = [
    pkgs.coreutils
    pkgs.gawk
    pkgs.gnused
    pkgs.gnugrep
    pkgs.util-linux
    pkgs.cryptsetup
    pkgs.e2fsprogs
    pkgs.inotify-tools
  ];
  lib = pkgs.lib;
  # facts.unlock_exit_<what> reaches the helper as YOLOBOX_UNLOCK_EXIT_<WHAT>.
  unlockExitCodes = lib.mapAttrs' (name: code: lib.nameValuePair "YOLOBOX_${lib.toUpper name}" (toString code))
    (lib.filterAttrs (name: _: lib.hasPrefix "unlock_exit_" name) facts);
  app = name: runtimeEnv: pkgs.writeShellApplication {
    inherit name runtimeEnv;
    runtimeInputs = inputs ++ [ systemd ];
    text = builtins.readFile (./. + "/${name}.sh");
  };
in
{
  inherit inputs;
  keys = app "yolobox-initrd-authorized-keys" { };
  unlock = app facts.initrd_unlock_helper ({
    YOLOBOX_UNLOCK_REPLY_PASSWORD = "${systemd}/lib/systemd/systemd-reply-password";
    YOLOBOX_UNLOCK_LUKS_DEVICE = facts.luks_device;
    YOLOBOX_ASK_DIR = facts.ask_password_dir;
    YOLOBOX_UNLOCK_UNIT = "systemd-cryptsetup@${facts.luks_mapper}.service";
    YOLOBOX_UNLOCK_ENCRYPT_UNIT = "${facts.luks_encrypt_unit}.service";
  } // unlockExitCodes);
  encrypt = app "yolobox-luks-encrypt" { };
}
