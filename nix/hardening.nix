{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.hardening;

  # The one spelling of the list; both settings below render from it.
  # Autoloading a protocol module needs only a socket() call by the agent,
  # and most network-protocol kernel CVEs arrive that way. vsock, virtiofs,
  # binfmt_misc, overlay, fuse, tun, bridge, veth, nf_*/xt_*, dm_crypt,
  # af_alg, sch_fq_codel and the filesystems must stay loadable. See nix/hardening.md.
  blockedModules = [
    # wireless and bluetooth: vz has no such hardware
    "cfg80211" "mac80211" "bluetooth" "btusb"
    # rare socket families
    "sctp" "rds" "tipc" "x25" "psnap" "llc2" "can" "atm" "ieee802154"
    "phonet" "nfc" "af_key"
    # more socket families that autoload from a socket() call
    "rxrpc" "kcm" "smc" "qrtr" "pppoe" "l2tp_ppp"
    # net/sched: a user namespace's root can attach any of these to its own
    # netns. Nothing on the box shapes traffic; sch_fq_codel, the default
    # qdisc, is the one that stays.
    "cls_basic" "cls_bpf" "cls_cgroup" "cls_flow" "cls_flower" "cls_fw"
    "cls_matchall" "cls_route" "cls_u32"
    "act_bpf" "act_connmark" "act_csum" "act_ct" "act_ctinfo" "act_gact"
    "act_gate" "act_ife" "act_meta_mark" "act_meta_skbprio"
    "act_meta_skbtcindex" "act_mirred" "act_mpls" "act_nat" "act_pedit"
    "act_police" "act_sample" "act_simple" "act_skbedit" "act_skbmod"
    "act_tunnel_key" "act_vlan"
    "sch_cake" "sch_cbs" "sch_choke" "sch_codel" "sch_drr" "sch_dualpi2"
    "sch_etf" "sch_ets" "sch_fq" "sch_fq_pie" "sch_gred" "sch_hfsc"
    "sch_hhf" "sch_htb" "sch_ingress" "sch_mqprio" "sch_multiq" "sch_netem"
    "sch_pie" "sch_plug" "sch_prio" "sch_qfq" "sch_red" "sch_sfb" "sch_sfq"
    "sch_skbprio" "sch_taprio" "sch_tbf" "sch_teql"
  ];

  # Setuid and capability wrappers the box keeps, each with the privilege it
  # must carry. `capabilities` is the sorted list `getcap` prints, including
  # the cap_setpcap that nixpkgs adds to every capability wrapper; it is the
  # spelling tests/box-hardening-check.sh uses. See nix/hardening.md.
  setuidRoot = { setuid = true; capabilities = ""; };
  capRoot = capabilities: { setuid = false; inherit capabilities; };
  keptWrappers = {
    sudo = setuidRoot;
    sudoedit = setuidRoot;
    unix_chkpwd = setuidRoot;
    newuidmap = capRoot "cap_setpcap,cap_setuid";
    newgidmap = capRoot "cap_setgid,cap_setpcap";
  };
  expectedWrappers = keptWrappers // lib.mapAttrs
    (_: caps: if caps == "" then setuidRoot else capRoot caps)
    cfg.extraWrappers;

  enabledWrappers = lib.filterAttrs (_: w: w.enable) config.security.wrappers;
  enabledNames = lib.attrNames enabledWrappers;
  allowedNames = lib.attrNames expectedWrappers;
  unexpected = lib.subtractLists allowedNames enabledNames;
  missing = lib.subtractLists enabledNames allowedNames;
  shadowed = lib.attrNames (lib.intersectAttrs keptWrappers cfg.extraWrappers);

  # NixOS writes a wrapper's capabilities as one clause, "cap_a,cap_b+ep", and
  # its setcap adds cap_setpcap. This renders that as the sorted names getcap
  # prints. Any other clause form (several clauses, "all", numbers, "-") comes
  # out as "<unparsable ...>", which never equals an expected value.
  getcapSpelling = caps:
    let m = builtins.match "(cap_[a-z_]+(,cap_[a-z_]+)*)[+=][eip]+" caps; in
    if caps == "" then ""
    else if m == null then "<unparsable: ${caps}>"
    else lib.concatStringsSep ","
      (lib.sort lib.lessThan (lib.unique (lib.splitString "," (builtins.head m) ++ [ "cap_setpcap" ])));

  have = w: {
    inherit (w) program setuid setgid owner;
    capabilities = getcapSpelling w.capabilities;
  };
  want = name: e: {
    program = name;
    inherit (e) setuid capabilities;
    setgid = false;
    owner = "root";
  };
  wrongPrivilege = lib.filterAttrs
    (name: w: have w != want name expectedWrappers.${name})
    (lib.getAttrs (lib.intersectLists enabledNames allowedNames) enabledWrappers);

  # Refused from any source, an extraWrappers entry included. CAP_SYS_PTRACE
  # attaches to root's processes and hands the agent root (CONSTITUTION,
  # nix/base.md#same-uid-ptrace); CAP_SYS_ADMIN and the CAP_DAC_* pair reach
  # what other uids own.
  forbiddenCaps = lib.filterAttrs
    (_: w: lib.any (c: lib.hasInfix c (lib.toLower w.capabilities)) [ "cap_sys_ptrace" "cap_sys_admin" "cap_dac_" ])
    enabledWrappers;
in
{
  options.yolobox.hardening.extraWrappers = lib.mkOption {
    type = lib.types.attrsOf (lib.types.strMatching "(cap_[a-z_]+(,cap_[a-z_]+)*)?");
    default = { };
    example = { dumpcap = "cap_net_admin,cap_net_raw,cap_setpcap"; helper = ""; };
    description = ''
      Setuid or capability wrappers a box adds on top of the allowlist.
      Escape hatch for /etc/yolobox/local.nix, e.g. a VPN client's
      split-tunnel helper. The name is the wrapper's name; the value is
      the capabilities it must carry, spelled as `getcap` prints them:
      sorted, comma separated, including the cap_setpcap that nixpkgs adds.
      The empty string means a setuid-root wrapper with no capabilities.
      Evaluation fails when an enabled wrapper differs in program name,
      setuid, setgid, owner or capabilities.
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

    assertions = [
      {
        assertion = shadowed == [ ];
        message = "yolobox.hardening.extraWrappers names kept wrappers (${lib.concatStringsSep ", " shadowed}); the allowlist already fixes their privilege (nix/hardening.nix).";
      }
      {
        assertion = unexpected == [ ] && missing == [ ];
        message = lib.concatStringsSep "\n" (
          [ "security.wrappers differs from the allowlist (nix/hardening.nix)." ]
          ++ lib.optional (unexpected != [ ]) "Unexpected wrappers: ${lib.concatStringsSep ", " unexpected}"
          ++ lib.optional (missing != [ ]) "Missing wrappers: ${lib.concatStringsSep ", " missing}"
          ++ [ "Add a wanted one to yolobox.hardening.extraWrappers; see nix/hardening.md." ]
        );
      }
      {
        assertion = wrongPrivilege == { };
        message = lib.concatStringsSep "\n" (
          [ "A wrapper carries other privilege than the allowlist states (nix/hardening.nix)." ]
          ++ lib.mapAttrsToList
            (name: w: "${name}: have ${builtins.toJSON (have w)}, want ${builtins.toJSON (want name expectedWrappers.${name})}")
            wrongPrivilege
          ++ [ "Fix the wrapper, or state its privilege in yolobox.hardening.extraWrappers; see nix/hardening.md." ]
        );
      }
      {
        assertion = forbiddenCaps == { };
        message = lib.concatStringsSep "\n" (
          [ "A wrapper carries cap_sys_ptrace, cap_sys_admin or a cap_dac_* capability, which the box never allows (nix/hardening.nix):" ]
          ++ lib.mapAttrsToList (name: w: "${name}: ${w.capabilities}") forbiddenCaps
        );
      }
    ];

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

    # nixos-lima sets this to 0, so the agent could bind sshd's port 22 whenever
    # sshd is not holding it. Kept outside the block above because mkForce is
    # not an integer literal; tests/test_consistency.py reads both spellings.
    boot.kernel.sysctl."net.ipv4.ip_unprivileged_port_start" = lib.mkForce 1024;

    # kexec off and nohibernate; slab_nomerge and page_alloc.shuffle harden
    # the heap; debugfs=off removes a kernel-internals surface.
    security.protectKernelImage = true;
    boot.kernelParams = [ "slab_nomerge" "page_alloc.shuffle=1" "debugfs=off" ];
  };
}
