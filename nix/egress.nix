{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.network;
  nft = "${pkgs.nftables}/bin/nft";

  table = "inet yolobox-egress";
  tailnetIface = "tailscale0";

  # The first rule accepts what leaves by these. A tunnel table that a box's
  # local.nix adds DNATs DNS to an in-tunnel resolver, which sits in a private
  # range, and the rejects below would refuse it on any interface.
  # Like tailscale0, traffic out of a listed interface is not judged here.
  acceptedIfaces = [ "lo" tailnetIface ] ++ cfg.tunnelInterfaces;
  ifaceSet = lib.concatMapStringsSep ", " (name: "\"${name}\"") acceptedIfaces;
  # Like lanAllow, a bad name fails the evaluation and never reaches nft.
  badIfaces = builtins.filter (name: builtins.match "[a-zA-Z0-9_.-]{1,15}" name == null) cfg.tunnelInterfaces;

  # Lima's gateway is gvproxy's NAT to the host's own loopback, so it is the one
  # address on the host the guest may speak to, and only for DNS.
  limaGateway = "192.168.5.2";

  # Everything on the host or a private network the guest can address: RFC1918,
  # the Lima subnet inside it, CGNAT (tailscale0 is accepted before this is
  # judged), link-local, multicast and the limited broadcast.
  blockedV4 = "10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255";
  blockedV6 = "fc00::/7, fe80::/10, ff00::/8, 64:ff9b:1::/48";

  # Runs after the hooks that change a packet (mangle -150, dstnat -100), so
  # the destination judged here is the final one. The oif is not: every chain
  # of the output hook sees the interface of the route taken before the hook,
  # and a DNAT that reroutes does not update it (nix/egress.md). -20 is
  # strictly before another table at -10: with equal priorities the
  # kernel orders chains by load order, which is not fixed. Either table's
  # reject is final.
  hookPriority = "-20";

  logRate = "6/minute burst 3 packets";

  # lanAllow entries are checked here, so a typo fails the evaluation and never
  # reaches nft, where it would drop the box into recovery mode. No leading
  # zeros: inet_pton rejects them. An address with bits set past the prefix
  # (192.168.1.20/24) is refused too: nft would load it as the network it lies
  # in, 192.168.1.0/24, and open a whole LAN where one host was meant.
  cidrSplit = entry: builtins.match "([^/]+)/(0|[1-9][0-9]{0,2})" entry;
  octet = "(0|[1-9][0-9]{0,2})";
  v4Octets = addr: builtins.match "${octet}\\.${octet}\\.${octet}\\.${octet}" addr;
  v6Groups = part: if part == "" then [ ] else lib.splitString ":" part;
  pow2 = n: lib.foldl (x: _: x * 2) 1 (lib.range 1 n);
  # The low `bits` bits of `n` are zero.
  lowBitsClear = bits: n: builtins.bitAnd n (pow2 bits - 1) == 0;
  isV4Addr = addr:
    let octets = v4Octets addr;
    in octets != null && lib.all (o: builtins.fromJSON o <= 255) octets;
  v4Number = addr: lib.foldl (n: o: n * 256 + builtins.fromJSON o) 0 (v4Octets addr);
  isV4Network = addr: prefix: lowBitsClear (32 - prefix) (v4Number addr);
  # "::" stands for one or more zero groups, so a compressed address has at
  # most 7 written groups and an uncompressed one exactly 8.
  isV6Addr = addr:
    let
      parts = map v6Groups (lib.splitString "::" addr);
      groups = lib.concatLists parts;
    in
    lib.all (g: builtins.match "[0-9a-fA-F]{1,4}" g != null) groups
    && (if lib.length parts == 1 then lib.length groups == 8
        else lib.length parts == 2 && lib.length groups <= 7);
  # The eight groups as numbers, "::" expanded to the zero groups it stands for.
  v6Numbers = addr:
    let
      parts = map v6Groups (lib.splitString "::" addr);
      written = lib.concatLists parts;
      zeros = lib.genList (_: "0") (8 - lib.length written);
      groups = if lib.length parts == 1 then written
        else lib.head parts ++ zeros ++ lib.last parts;
    in map (g: (builtins.fromTOML "n = 0x${g}").n) groups;
  # The group the prefix ends in keeps its low bits clear, and every group
  # after it is zero.
  isV6Network = addr: prefix:
    lib.all lib.id (lib.imap0 (i: g:
      if i * 16 + 16 <= prefix then true
      else if i * 16 >= prefix then g == 0
      else lowBitsClear (i * 16 + 16 - prefix) g) (v6Numbers addr));
  isCidr = isAddr: isNetwork: maxPrefix: entry:
    let
      split = cidrSplit entry;
      addr = lib.elemAt split 0;
      prefix = builtins.fromJSON (lib.elemAt split 1);
    in split != null && isAddr addr && prefix <= maxPrefix && isNetwork addr prefix;
  isV4 = isCidr isV4Addr isV4Network 32;
  isV6 = isCidr isV6Addr isV6Network 128;
  lanV4 = builtins.filter isV4 cfg.lanAllow;
  lanV6 = builtins.filter isV6 cfg.lanAllow;
  notCidr = builtins.filter (entry: !(isV4 entry || isV6 entry)) cfg.lanAllow;

  # An empty nft set is a syntax error, so an empty list emits no rule at all.
  lanRules = lib.concatStringsSep "\n    "
    (lib.optional (lanV4 != [ ]) "ip daddr { ${lib.concatStringsSep ", " lanV4} } accept"
      ++ lib.optional (lanV6 != [ ]) "ip6 daddr { ${lib.concatStringsSep ", " lanV6} } accept");

  # With tunnel interfaces, a packet a DNAT rerouted into the tunnel would be
  # judged by the interface of its first route. So the output chain leaves a
  # rewritten packet to a postrouting chain, where the oif is the one it leaves
  # by, and judges it there: the same accepted interfaces, the same blocked
  # ranges, and drop, because reject is not allowed in postrouting. Without
  # tunnel interfaces nothing is rendered: the box rewrites nothing.
  hasTunnels = cfg.tunnelInterfaces != [ ];
  rewrittenDefer = lib.optionalString hasTunnels "\n    ct status dnat accept";
  rewrittenChain = lib.optionalString hasTunnels (lib.concatMapStrings (line: "\n  " + line) [
    "chain postrouting {"
    "  type filter hook postrouting priority ${hookPriority}; policy accept;"
    "  ct direction reply accept"
    "  ct status dnat oifname { ${ifaceSet} } accept"
    "  ct status dnat ip daddr { ${blockedV4} } limit rate ${logRate} log prefix \"yolobox-egress: blocked: \" level warn"
    "  ct status dnat ip daddr { ${blockedV4} } counter drop"
    "  ct status dnat ip6 daddr { ${blockedV6} } limit rate ${logRate} log prefix \"yolobox-egress: blocked: \" level warn"
    "  ct status dnat ip6 daddr { ${blockedV6} } counter drop"
    "}"
  ]);

  # One atomic nft transaction: `table`, `delete table` and `table {..}` in one
  # file, so a reload swaps the rules with no instant at which the table is
  # missing. builtins.toFile, not pkgs.writeText: the text names no store path,
  # and toFile writes the file at eval time, so CI can `nft --check` it on any
  # runner without building anything (.github/workflows/verify.yml).
  ruleFile = builtins.toFile "yolobox-egress.nft" ''
    table ${table}
    delete table ${table}
    table ${table} {
      chain output {
        type filter hook output priority ${hookPriority}; policy accept;
        oifname { ${ifaceSet} } accept
        ct direction reply accept${rewrittenDefer}
        udp sport 68 udp dport 67 ip daddr { 255.255.255.255, ${limaGateway} } accept
        ip daddr ${limaGateway} udp dport 53 accept
        ip daddr ${limaGateway} tcp dport 53 accept
        ${lanRules}
        icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
        # MLD reports go to link-local groups; ff02::/16 stops the accept passing one off the link.
        icmpv6 type { mld-listener-report, mld2-listener-report } ip6 daddr ff02::/16 accept
        ip daddr { ${blockedV4} } limit rate ${logRate} log prefix "yolobox-egress: blocked: " level warn
        ip daddr { ${blockedV4} } counter reject with icmpx type admin-prohibited
        ip6 daddr { ${blockedV6} } limit rate ${logRate} log prefix "yolobox-egress: blocked: " level warn
        ip6 daddr { ${blockedV6} } counter reject with icmpx type admin-prohibited
      }${rewrittenChain}
    }
  '';

  # What the box runs in when ruleFile fails to load: no network the guest
  # starts, only DHCP and the replies that keep Lima's inbound ssh alive. It
  # names nothing from the options, so no local.nix can break it. Its chain
  # name tells `yo status` which tier is loaded; so does the last-resort one.
  recoveryFile = builtins.toFile "yolobox-egress-recovery.nft" ''
    table ${table}
    delete table ${table}
    table ${table} {
      chain output_recovery {
        type filter hook output priority ${hookPriority}; policy accept;
        oifname "lo" accept
        ct direction reply accept
        udp sport 68 udp dport 67 ip daddr { 255.255.255.255, ${limaGateway} } accept
        icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
        limit rate ${logRate} log prefix "yolobox-egress: recovery: " level warn
        counter reject with icmpx type admin-prohibited
      }
    }
  '';

  # What the box runs in when even recoveryFile fails to load, which happens
  # when nft or the kernel lacks one of the statements it shares with ruleFile
  # (log, limit rate, ct direction, icmpx reject). The statements here are the
  # oldest ones nft has, and the chain drops by policy, so the file has no
  # reject to get wrong. `ct state established,related` rather than `ct
  # direction reply` for the same reason: it also lets a flow the guest opened
  # earlier keep sending (nix/egress.md). DHCP names no destination: the agent
  # cannot bind port 68 (nix/hardening.nix), and a set is one more thing to
  # fail. Without this tier a box whose nft loads nothing runs with no table
  # at all.
  lastResortFile = builtins.toFile "yolobox-egress-lastresort.nft" ''
    table ${table}
    delete table ${table}
    table ${table} {
      chain output_lastresort {
        type filter hook output priority ${hookPriority}; policy drop;
        oifname "lo" accept
        ct state established,related accept
        udp sport 68 udp dport 67 accept
      }
    }
  '';

  loadTiers = pkgs.writeShellApplication {
    name = "yolobox-egress-load-tiers";
    runtimeInputs = [ pkgs.nftables ];
    text = builtins.readFile ./egress/yolobox-egress-load-tiers.sh;
  };

  reportFailure = pkgs.writeShellApplication {
    name = "yolobox-egress-failed";
    runtimeInputs = [ pkgs.nftables ];
    text = builtins.readFile ./egress/yolobox-egress-failed.sh;
  };
in
{
  options.yolobox.network.lanAllow = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    example = [ "192.168.1.20/32" ];
    description = ''
      Private networks the guest may reach, e.g. a LAN package cache. Empty:
      no LAN, no host. Name single addresses where you can: a network that
      holds the host's own LAN address lets the guest reach the host.
      On a Linux host it does nothing: the host table rejects the same
      ranges for the guest (docs/linux-host.md).
    '';
  };

  options.yolobox.network.tunnelInterfaces = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    example = [ "wg0" ];
    description = ''
      Interfaces whose outgoing traffic this table does not judge, besides lo
      and tailscale0. For a box whose local.nix sends egress through a tunnel and
      rejects what bypasses it: the tunnel is then the gate.
    '';
  };

  config = {
    assertions = [
      {
        assertion = notCidr == [ ];
        message = "yolobox.network.lanAllow: not a CIDR: ${lib.concatStringsSep ", " notCidr}";
      }
      {
        assertion = badIfaces == [ ];
        message = "yolobox.network.tunnelInterfaces: not an interface name (1 to 15 of A-Z a-z 0-9 _ . -): ${lib.concatStringsSep ", " badIfaces}";
      }
      {
        assertion = lib.unique acceptedIfaces == acceptedIfaces;
        message = "yolobox.network.tunnelInterfaces: repeats an interface, or names lo or ${tailnetIface}, which are always accepted: ${lib.concatStringsSep ", " cfg.tunnelInterfaces}";
      }
    ];

    system.build.egressRules = ruleFile;
    system.build.egressRecoveryRules = recoveryFile;
    system.build.egressLastResortRules = lastResortFile;

    # The recovery steps in egress.md run nft by name.
    environment.systemPackages = [ pkgs.nftables ];

    # A separate nft table, not networking.nftables: that option would switch
    # the firewall backend, and nixos-fw stays on iptables-nft. No ExecStop:
    # stopping the unit, or a switch that restarts it, must never open the
    # box. ExecStartPre loads the recovery table first (the last-resort one if
    # that fails), so when ExecStart fails the unit is failed and the fallback
    # table stays. When no table loads at all the unit fails the same way, and
    # OnFailure says so loudly.
    systemd.services.yolobox-egress = {
      description = "Reject guest traffic to the host and private networks";
      wantedBy = [ "sysinit.target" ];
      before = [ "network-pre.target" "sysinit.target" ];
      unitConfig.DefaultDependencies = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStartPre = [ "${loadTiers}/bin/yolobox-egress-load-tiers ${recoveryFile} ${lastResortFile}" ];
        ExecStart = "${nft} --file ${ruleFile}";
      };
    };

    # Not wantedBy anything: only a failure of yolobox-egress starts it. No
    # default dependencies, so it also runs in early boot, before basic.target.
    systemd.services.yolobox-egress-failed = {
      description = "Report that the egress rules failed to load";
      unitConfig.DefaultDependencies = false;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${reportFailure}/bin/yolobox-egress-failed";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };
    systemd.services.yolobox-egress.onFailure = [ "yolobox-egress-failed.service" ];

    # tailscaled refuses to start while the table is absent. Wants= starts the
    # egress unit in the same transaction and After= orders it first.
    # ExecStartPre is the gate, and it holds even when the unit failed. Not
    # Requires=: that would restart tailscaled whenever a switch restarts the
    # egress unit. `+` runs the check as root, outside the unit's sandbox. The
    # list merges with any other module's gate.
    systemd.services.tailscaled = {
      wants = [ "yolobox-egress.service" ];
      after = [ "yolobox-egress.service" ];
      serviceConfig.ExecStartPre = [ "+${nft} list table ${table}" ];
    };

    # dhcpcd is ordered after the egress unit but never gated on it: without an
    # address there is no Lima ssh, so no way back in to repair the rules.
    systemd.services.dhcpcd = {
      wants = [ "yolobox-egress.service" ];
      after = [ "yolobox-egress.service" ];
    };
    # The table rejects ff00::/8, so DHCPv6 to ff02::1:2 never leaves, and
    # dhcpcd logged "sendmsg: Operation not permitted" for every try. The
    # address comes from router advertisements (SLAAC) either way.
    networking.dhcpcd.extraConfig = "nodhcp6";
  };
}
