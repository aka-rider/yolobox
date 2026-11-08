{ config, lib, pkgs, facts, ... }:
let
  # Autoloading a protocol module needs only a socket() call by the agent,
  # and most network-protocol kernel CVEs arrive that way. vsock, virtiofs,
  # binfmt_misc, overlay, fuse, tun, bridge, veth, nf_*/xt_*, dm_crypt,
  # af_alg, sch_fq_codel and the filesystems must stay loadable.
  blockedModules = [
    "cfg80211" "mac80211" # wireless: the VM has no such hardware, so the modules are only loadable code
    "bluetooth" "btusb" # no bluetooth hardware either, and AF_BLUETOOTH autoloads from socket() with a long CVE history
    "sctp" "rds" "tipc" # rare transports that autoload from socket(); nothing on the box speaks them
    "x25" "psnap" "llc2" "atm" # legacy link and network layers, unused
    "can" "ieee802154" "phonet" "nfc" # fieldbus, radio and modem families; a VM has none of the hardware
    "af_key" # the PF_KEY IPsec socket; tailscale is WireGuard in userspace, nothing here uses kernel IPsec
    "mctp" # AF_MCTP, a management bus for BMCs; a VM has no such bus
    "rxrpc" "kcm" "smc" "qrtr" # AF_RXRPC, AF_KCM, AF_SMC and AF_QIPCRTR autoload from socket(); AFS, message framing, IBM SMC and Qualcomm modems, none used here
    "pppoe" "l2tp_ppp" "pptp" # the AF_PPPOX protocols; the box makes no PPP link, and pptp parses every GRE packet once loaded
    "l2tp_core" # what l2tp_ppp, l2tp_ip and l2tp_ip6 sit on; the last two autoload from socket(IPPROTO_L2TP) and have a use-after-free history (CVE-2023-4206)
    "scsi_transport_iscsi" # NETLINK_ISCSI autoloads from socket() and its parser gave root to any user (CVE-2021-27365); the root disk is virtio-blk
    "xfrm_user" # NETLINK_XFRM, the IPsec policy socket, with its own CVE history (CVE-2024-27404); nothing here uses kernel IPsec
    # link kinds `ip link add type <kind>` autoloads, which the agent can run as root of its own user and network namespace;
    # each is a packet parser the box never configures (podman uses bridge and veth, tailscale uses tun)
    "vxlan" "geneve" "bareudp" "gtp" "hsr" "macsec" "bonding" "team" "ipvlan"
    # net/sched: a user namespace's root can attach any of these to its own netns, and nothing on the box shapes traffic
    # classifiers: cls_route gave root (CVE-2022-2588), cls_u32 and cls_fw have the same kind of history
    "cls_basic" "cls_bpf" "cls_cgroup" "cls_flow" "cls_flower" "cls_fw" "cls_matchall" "cls_route" "cls_u32"
    # actions: act_* code runs on every packet a classifier matches; act_ct and act_nat depend on conntrack
    # and not the other way round, so blocking them leaves nf_conntrack and the egress table alone
    "act_bpf" "act_connmark" "act_csum" "act_ct" "act_ctinfo" "act_gact" "act_gate" "act_ife"
    "act_meta_mark" "act_meta_skbprio" "act_meta_skbtcindex" "act_mirred" "act_mpls" "act_nat"
    "act_pedit" "act_police" "act_sample" "act_simple" "act_skbedit" "act_skbmod" "act_tunnel_key" "act_vlan"
    # every qdisc but sch_fq_codel, the default: sch_qfq (CVE-2023-3611), sch_hfsc (CVE-2023-4623), sch_netem,
    # sch_sfb and sch_tbf have a history; sch_ingress also provides clsact, used only by tc and BPF tools the box lacks
    "sch_cake" "sch_cbs" "sch_choke" "sch_codel" "sch_drr" "sch_dualpi2" "sch_etf" "sch_ets" "sch_fq"
    "sch_fq_pie" "sch_gred" "sch_hfsc" "sch_hhf" "sch_htb" "sch_ingress" "sch_mqprio" "sch_multiq"
    "sch_netem" "sch_pie" "sch_plug" "sch_prio" "sch_qfq" "sch_red" "sch_sfb" "sch_sfq" "sch_skbprio"
    "sch_taprio" "sch_tbf" "sch_teql"
  ];

  # The wrappers the box keeps, each exactly as nixpkgs' own module makes it.
  # A capability wrapper ignores `setuid` and takes its mode from
  # `permissions`, so the mode string is compared too. sudo's group and mode
  # are what execWheelOnly makes of them, stated here and not read from it.
  # `capabilities` is what getcap prints: nixpkgs' setcap adds cap_setpcap.
  open = { group = "root"; permissions = "u+rx,g+x,o+x"; };
  wheelOnly = { group = "wheel"; permissions = "u+rx,g+x"; };
  setuidRoot = source: open // { inherit source; setuid = true; capabilities = ""; };
  capRoot = source: capabilities: open // { inherit source capabilities; setuid = false; };
  sudoBin = "${config.security.sudo.package.out}/bin";
  shadowBin = "${config.security.loginDefs.package.out}/bin";
  keptWrappers = lib.mapAttrs (name: w: w // { program = name; setgid = false; owner = "root"; }) {
    sudo = setuidRoot "${sudoBin}/sudo" // wheelOnly; # the operator's way to root; the agent cannot exec it
    sudoedit = setuidRoot "${sudoBin}/sudoedit" // wheelOnly;
    unix_chkpwd = setuidRoot "${config.security.pam.package}/bin/unix_chkpwd"; # PAM's password helper, called by sudo and the login stack
    newuidmap = capRoot "${shadowBin}/newuidmap" "cap_setpcap,cap_setuid"; # rootless podman maps subuids with these two, as file
    newgidmap = capRoot "${shadowBin}/newgidmap" "cap_setgid,cap_setpcap"; # capabilities and not setuid root, so a bug in them gains less
  };
  sortedCaps = names: lib.concatStringsSep "," (lib.sort lib.lessThan (lib.unique names));
  # NixOS writes a wrapper's capabilities as one clause, "cap_a,cap_b+ep", and
  # its setcap adds cap_setpcap. This is what getcap then prints, sorted. Any
  # other clause form ("all", numbers, several clauses) never matches a kept value.
  getcapNames = caps:
    let m = builtins.match "(cap_[a-z_]+(,cap_[a-z_]+)*)[+=][eip]+" caps; in
    if caps == "" then ""
    else if m == null then "<unparsable: ${caps}>"
    else sortedCaps (lib.splitString "," (builtins.head m) ++ [ "cap_setpcap" ]);
  privilege = w: {
    inherit (w) program setuid setgid owner group permissions;
    source = toString w.source;
    capabilities = getcapNames w.capabilities;
  };
  enabledWrappers = lib.mapAttrs (_: privilege) (lib.filterAttrs (_: w: w.enable) config.security.wrappers);
  wheelOnlySudo = config.security.sudo.execWheelOnly;

  # The agent's groups, all of them: no wheel (sudo), no systemd-journal or
  # adm (the system journal holds the operator's sudo lines), no podman (its
  # socket group is root-equivalent).
  agentGroups = [ "users" ];
  agentAccount = config.users.users.${facts.agent_user};
  sortedNames = names: lib.sort lib.lessThan (lib.unique names);
  agentHeldGroups = sortedNames ([ agentAccount.group ]
    ++ lib.attrNames (lib.filterAttrs (_: g: lib.elem facts.agent_user g.members) config.users.groups));

  sysctls = {
    "kernel.kptr_restrict" = 2;
    "kernel.unprivileged_bpf_disabled" = 1;
    "net.core.bpf_jit_harden" = 2;
    "kernel.io_uring_disabled" = 2;
    "dev.tty.ldisc_autoload" = 0;
    "dev.tty.legacy_tiocsti" = 0;
    "fs.protected_fifos" = 2;
    "fs.protected_regular" = 2;
    "fs.suid_dumpable" = 0;
    # security.protectKernelImage sets it too; stated here so the box's facts carry it
    "kernel.kexec_load_disabled" = 1;
    # strace -p on the agent's own processes must keep working
    "kernel.yama.ptrace_scope" = 0;
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
    # nixos-lima sets 0, so the agent could bind sshd's port 22 whenever sshd is not holding it
    "net.ipv4.ip_unprivileged_port_start" = 1024;
  };

  # /proc with hidepid: the agent sees no pid but its own, so it cannot read
  # the operator's or root's cmdline, environ or fds while a command runs.
  # gid= names the one group that still sees every pid, wheel, so the
  # operator's tools work as before. nixpkgs' nosuid,noexec,nodev merge in.
  procOptions = [ "hidepid=invisible" "gid=${toString config.users.groups.wheel.gid}" ];

  # What the acceptance check inside the box compares against, taken from the
  # values asserted above so that the two never drift; base.nix renders them
  # into /etc/yolobox/facts.sh beside facts.json's.
  computedFacts = {
    YOLOBOX_AGENT_GROUPS = lib.concatStringsSep " " agentGroups;
    YOLOBOX_WRAPPERS = lib.concatStringsSep " " (lib.mapAttrsToList (name: w: "${name}=${w.capabilities}") keptWrappers);
    YOLOBOX_SYSCTLS = lib.concatStringsSep " " (lib.mapAttrsToList (key: value: "${key}=${toString value}") sysctls);
    YOLOBOX_KERNEL_PARAMS = lib.concatStringsSep " " config.boot.kernelParams;
    YOLOBOX_PROC_OPTIONS = lib.concatStringsSep " " config.boot.specialFileSystems."/proc".options;
  };
in
{
  config = {
    # blacklist stops alias autoload; install-false stops an explicit
    # modprobe. NixOS has no /bin/false, hence the store path.
    boot.blacklistedKernelModules = blockedModules;
    boot.extraModprobeConfig = lib.concatMapStringsSep "\n"
      (m: "install ${m} ${pkgs.coreutils}/bin/false") blockedModules;

    # The agent has no password and root is locked, so these only add
    # privilege-escalation paths: su, sg and newgrp switch identity by
    # password, chsh and passwd change the account database, and a setuid
    # mount is a classic escalation through fstab and option parsing.
    security.wrappers = lib.genAttrs
      [ "su" "sg" "newgrp" "chsh" "passwd" "mount" "umount" ]
      (_: { enable = false; });

    # NixOS remounts a special filesystem on switch, so this needs no reboot.
    boot.specialFileSystems."/proc".options = procOptions;
    # logind reads /proc/<pid> of every session it tracks. CAP_SYS_PTRACE is
    # what exempts a process from hidepid, and logind's unit drops it from
    # its bounding set, so logind takes the gid= group instead.
    systemd.services.systemd-logind.serviceConfig.SupplementaryGroups = [ "wheel" ];

    assertions = [
      {
        assertion = enabledWrappers == keptWrappers && wheelOnlySudo
          && agentHeldGroups == sortedNames agentGroups;
        message = lib.concatStringsSep "\n" [
          "The box keeps exactly these setuid and capability wrappers, each wrapping exactly this binary with exactly this privilege, only wheel may exec sudo, and the agent holds exactly these groups."
          "wrappers: have ${builtins.toJSON enabledWrappers}"
          "          want ${builtins.toJSON keptWrappers}"
          "security.sudo.execWheelOnly: have ${lib.boolToString wheelOnlySudo}, want true"
          "agent groups: have ${lib.concatStringsSep ", " agentHeldGroups}, want ${lib.concatStringsSep ", " agentGroups}"
        ];
      }
    ];

    # mkForce: nixpkgs and nixos-lima set some of these keys at a lower value,
    # and the hardened value must win on every box.
    boot.kernel.sysctl = lib.mapAttrs (_: lib.mkForce) sysctls;

    # kexec off and nohibernate; slab_nomerge and page_alloc.shuffle harden
    # the heap; debugfs=off removes a kernel-internals surface.
    security.protectKernelImage = true;
    boot.kernelParams = [ "slab_nomerge" "page_alloc.shuffle=1" "debugfs=off" ];

    yolobox.facts = computedFacts;
  };
}
