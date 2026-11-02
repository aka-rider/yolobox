{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.hardening;

  # The one spelling of the list; both settings below render from it.
  # Autoloading a protocol module needs only a socket() call by the agent,
  # and most network-protocol kernel CVEs arrive that way. vsock, virtiofs,
  # binfmt_misc, overlay, fuse, tun, bridge, veth, nf_*/xt_*, dm_crypt,
  # af_alg and the filesystems must stay loadable. See nix/hardening.md.
  blockedModules = [
    # wireless and bluetooth: vz has no such hardware
    "cfg80211" "mac80211" "bluetooth" "btusb"
    # rare socket families
    "sctp" "rds" "tipc" "x25" "psnap" "llc2" "can" "atm" "ieee802154"
    "phonet" "nfc" "af_key"
    # more socket families that autoload from a socket() call
    "rxrpc" "kcm" "smc" "qrtr" "pppoe" "l2tp_ppp"
  ];

  # Setuid and capability wrappers the box keeps. See nix/hardening.md.
  keptWrappers = [ "sudo" "sudoedit" "unix_chkpwd" "newuidmap" "newgidmap" ];
  enabledWrappers = lib.attrNames (lib.filterAttrs (_: w: w.enable) config.security.wrappers);
  allowedWrappers = keptWrappers ++ cfg.extraWrappers;
  unexpected = lib.subtractLists allowedWrappers enabledWrappers;
  missing = lib.subtractLists enabledWrappers allowedWrappers;
in
{
  options.yolobox.hardening.extraWrappers = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = ''
      Setuid or capability wrappers a box adds on top of the allowlist.
      Escape hatch for /etc/yolobox/local.nix, e.g. a VPN client's
      split-tunnel helper.
    '';
  };

  config = {
    # blacklist stops alias autoload; install-false stops an explicit
    # modprobe. NixOS has no /bin/false, hence the store path.
    boot.blacklistedKernelModules = blockedModules;
    boot.extraModprobeConfig = lib.concatMapStringsSep "\n"
      (m: "install ${m} ${pkgs.coreutils}/bin/false") blockedModules;

    # The agent has no password and root is locked, so these only add
    # privilege-escalation paths. See nix/hardening.md.
    security.wrappers = lib.genAttrs
      [ "su" "sg" "newgrp" "chsh" "passwd" "mount" "umount" ]
      (_: { enable = false; });

    assertions = [{
      assertion = unexpected == [ ] && missing == [ ];
      message = lib.concatStringsSep "\n" (
        [ "security.wrappers differs from the allowlist (nix/hardening.nix)." ]
        ++ lib.optional (unexpected != [ ]) "Unexpected wrappers: ${lib.concatStringsSep ", " unexpected}"
        ++ lib.optional (missing != [ ]) "Missing wrappers: ${lib.concatStringsSep ", " missing}"
        ++ [ "Add a wanted one to yolobox.hardening.extraWrappers; see nix/hardening.md." ]
      );
    }];

    # See nix/hardening.md for each line.
    boot.kernel.sysctl = {
      "kernel.kptr_restrict" = 2; # beats nixpkgs' mkDefault 1 at normal priority
      "kernel.unprivileged_bpf_disabled" = 1;
      "net.core.bpf_jit_harden" = 2;
      "kernel.io_uring_disabled" = 2;
      "dev.tty.ldisc_autoload" = 0;
      "fs.protected_fifos" = 2;
      "fs.protected_regular" = 2;
      "fs.suid_dumpable" = 0;
      "net.ipv4.conf.all.accept_redirects" = 0;
      "net.ipv4.conf.default.accept_redirects" = 0;
      "net.ipv4.conf.all.secure_redirects" = 0;
      "net.ipv4.conf.default.secure_redirects" = 0;
      "net.ipv4.conf.all.send_redirects" = 0;
      "net.ipv4.conf.default.send_redirects" = 0;
      "net.ipv4.conf.all.accept_source_route" = 0;
      "net.ipv4.conf.default.accept_source_route" = 0;
      "net.ipv6.conf.all.accept_redirects" = 0;
      "net.ipv6.conf.default.accept_redirects" = 0;
      "net.ipv6.conf.all.accept_source_route" = 0;
      "net.ipv6.conf.default.accept_source_route" = 0;
    };

    # kexec off and nohibernate; slab_nomerge and page_alloc.shuffle harden
    # the heap; debugfs=off removes a kernel-internals surface.
    security.protectKernelImage = true;
    boot.kernelParams = [ "slab_nomerge" "page_alloc.shuffle=1" "debugfs=off" ];
  };
}
