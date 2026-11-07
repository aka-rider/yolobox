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
    "phonet" "nfc" "af_key" "mctp"
    # more socket families that autoload from a socket() call
    "rxrpc" "kcm" "smc" "qrtr" "pppoe" "l2tp_ppp" "pptp" "l2tp_core"
    # netlink families that autoload from a socket() call
    "scsi_transport_iscsi" "xfrm_user"
    # link kinds a user namespace's root can create with `ip link add`;
    # bridge, veth and tun stay (podman, tailscale)
    "vxlan" "geneve" "bareudp" "gtp" "hsr" "macsec" "bonding" "team" "ipvlan"
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
  # must carry and the binary it must wrap. `capabilities` is the comma
  # separated list `getcap` prints, including the cap_setpcap that nixpkgs
  # adds to every capability wrapper. It is compared sorted, so a value may
  # list its names in any order. `source` is the path nixpkgs' own module
  # gives the wrapper (security/sudo.nix, security/pam.nix,
  # programs/shadow.nix), so a kept name pointed at another binary fails the
  # rebuild. See nix/hardening.md.
  #
  # `group` and `permissions` are compared too. A capability wrapper ignores
  # its `setuid` option and takes its mode from `permissions` alone, so a
  # permissions string with an s would make it setuid. `open` is what nixpkgs
  # gives a wrapper by default (the default of `permissions` in
  # security/wrappers/default.nix). `wheelOnly` is what
  # security.sudo.execWheelOnly makes of sudo's wrappers (security/sudo.nix),
  # stated here and not read from that option, so that switching the option
  # off fails the rebuild.
  groupOnly = "u+rx,g+x";
  open = { group = "root"; permissions = "u+rx,g+x,o+x"; };
  wheelOnly = { group = "wheel"; permissions = groupOnly; };
  setuidRoot = open // { setuid = true; capabilities = ""; };
  capRoot = capabilities: open // { setuid = false; inherit capabilities; };
  wrapping = source: { inherit source; };
  sudoBin = "${config.security.sudo.package.out}/bin";
  shadowBin = "${config.security.loginDefs.package.out}/bin";
  keptWrappers = {
    sudo = setuidRoot // wheelOnly // wrapping "${sudoBin}/sudo";
    sudoedit = setuidRoot // wheelOnly // wrapping "${sudoBin}/sudoedit";
    unix_chkpwd = setuidRoot // wrapping "${config.security.pam.package}/bin/unix_chkpwd";
    newuidmap = capRoot "cap_setpcap,cap_setuid" // wrapping "${shadowBin}/newuidmap";
    newgidmap = capRoot "cap_setgid,cap_setpcap" // wrapping "${shadowBin}/newgidmap";
  };
  # The one spelling of a capability list that both sides are compared in:
  # unique, byte-sorted, comma separated. "" has no names and stays "".
  sortedCaps = names: lib.concatStringsSep "," (lib.sort lib.lessThan (lib.unique names));
  expectedWrappers = keptWrappers // lib.mapAttrs
    (_: caps: if caps == "" then setuidRoot else capRoot (sortedCaps (lib.splitString "," caps)))
    cfg.extraWrappers;

  enabledWrappers = lib.filterAttrs (_: w: w.enable) config.security.wrappers;
  enabledNames = lib.attrNames enabledWrappers;
  allowedNames = lib.attrNames expectedWrappers;
  unexpected = lib.subtractLists allowedNames enabledNames;
  missing = lib.subtractLists enabledNames allowedNames;
  shadowed = lib.attrNames (lib.intersectAttrs keptWrappers cfg.extraWrappers);

  # A comma separated list of capability names, the one spelling both the
  # extraWrappers option type and getcapSpelling match.
  capList = "(cap_[a-z_]+(,cap_[a-z_]+)*)";

  # NixOS writes a wrapper's capabilities as one clause, "cap_a,cap_b+ep", and
  # its setcap adds cap_setpcap. This renders that as the sorted names getcap
  # prints. Any other clause form (several clauses, "all", numbers, "-") comes
  # out as "<unparsable ...>", which never equals an expected value.
  getcapSpelling = caps:
    let m = builtins.match "${capList}[+=][eip]+" caps; in
    if caps == "" then ""
    else if m == null then "<unparsable: ${caps}>"
    else sortedCaps (lib.splitString "," (builtins.head m) ++ [ "cap_setpcap" ]);

  have = w: {
    inherit (w) program setuid setgid owner group permissions;
    source = toString w.source;
    capabilities = getcapSpelling w.capabilities;
  };
  want = name: e: {
    program = name;
    inherit (e) setuid capabilities group permissions source;
    setgid = false;
    owner = "root";
  };
  # An extraWrappers entry names no source: the box's own module gives it one,
  # and the basename rule below is what checks it. It may also be narrower
  # than `open`: with mode u+rx,g+x only root and one group can exec it
  # (nixpkgs' wireshark gives dumpcap that), which is never wider than
  # `open`, so its group is taken as given.
  expected = name: w:
    let e = expectedWrappers.${name}; in
    if keptWrappers ? ${name} then e
    else e // { source = toString w.source; }
      // lib.optionalAttrs (w.permissions == groupOnly) { inherit (w) group permissions; };
  wrongPrivilege = lib.filterAttrs
    (name: w: have w != want name (expected name w))
    (lib.getAttrs (lib.intersectLists enabledNames allowedNames) enabledWrappers);
  # An extra wrapper must wrap the binary of its own name: `dumpcap` wrapping
  # bash would carry dumpcap's capabilities into a shell.
  misnamedSource = lib.filterAttrs
    (name: w: baseNameOf (toString w.source) != name)
    (lib.getAttrs (lib.intersectLists enabledNames (lib.attrNames cfg.extraWrappers)) enabledWrappers);

  # Refused from any source, an extraWrappers entry included. CAP_SYS_PTRACE
  # attaches to root's processes and hands the agent root (CONSTITUTION,
  # nix/base.md#same-uid-ptrace); the rest each reach root in one step: see the
  # capability table in nix/hardening.md. cap_setuid and cap_setgid are root
  # by another name, and only the two id-map helpers carry one each.
  forbiddenCapNames = [
    "cap_sys_ptrace" "cap_sys_admin" "cap_dac_" "cap_sys_module" "cap_sys_rawio"
    "cap_setfcap" "cap_fowner" "cap_chown" "cap_bpf" "cap_sys_boot" "cap_mknod"
  ];
  idMapWrappers = [ "newuidmap" "newgidmap" ];
  forbiddenCapsOf = name:
    forbiddenCapNames ++ lib.optionals (!(lib.elem name idMapWrappers)) [ "cap_setuid" "cap_setgid" ];
  forbiddenCaps = lib.filterAttrs
    (name: w: lib.any (c: lib.hasInfix c (lib.toLower w.capabilities)) (forbiddenCapsOf name))
    enabledWrappers;
in
{
  options.yolobox.hardening.extraWrappers = lib.mkOption {
    type = lib.types.attrsOf (lib.types.strMatching "${capList}?");
    default = { };
    example = { dumpcap = "cap_net_admin,cap_net_raw,cap_setpcap"; helper = ""; };
    description = ''
      Setuid or capability wrappers a box adds on top of the allowlist.
      Escape hatch for /etc/yolobox/local.nix, e.g. a VPN client's
      split-tunnel helper. The name is the wrapper's name; the value is
      the capabilities it must carry: comma separated, in any order, as
      `getcap` lists them (include the cap_setpcap that nixpkgs adds).
      The empty string means a setuid-root wrapper with no capabilities.
      The wrapper must be owned by root with nixpkgs' default permissions
      and group root, or with the permissions `u+rx,g+x` and any group,
      and its source must be a binary of the wrapper's own name.
      Evaluation fails when an enabled wrapper differs in program name,
      source, setuid, setgid, owner, group, permissions or capabilities.
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
            (name: w: "${name}: have ${builtins.toJSON (have w)}, want ${builtins.toJSON (want name (expected name w))}")
            wrongPrivilege
          ++ [ "Fix the wrapper, or state its privilege in yolobox.hardening.extraWrappers; see nix/hardening.md." ]
        );
      }
      {
        assertion = misnamedSource == { };
        message = lib.concatStringsSep "\n" (
          [ "An extra wrapper wraps a binary of another name, which the box never allows (nix/hardening.nix):" ]
          ++ lib.mapAttrsToList (name: w: "${name}: source ${toString w.source}") misnamedSource
        );
      }
      {
        assertion = forbiddenCaps == { };
        message = lib.concatStringsSep "\n" (
          [ "A wrapper carries a capability the box never allows (${lib.concatStringsSep ", " forbiddenCapNames}, or cap_setuid/cap_setgid outside ${lib.concatStringsSep "/" idMapWrappers}; nix/hardening.nix):" ]
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
