{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.network;
  nft = "${pkgs.nftables}/bin/nft";

  table = "inet yolobox-egress";
  tailnetIface = "tailscale0";

  # Lima's gateway is gvproxy's NAT to the host's own loopback, so it is the one
  # address on the host the guest may speak to, and only for DNS.
  limaGateway = "192.168.5.2";

  # Everything on the host or a private network the guest can address: RFC1918,
  # the Lima subnet inside it, CGNAT (tailscale0 is accepted before this is
  # judged), link-local, multicast and the limited broadcast.
  blockedV4 = "10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255";
  blockedV6 = "fc00::/7, fe80::/10, ff00::/8";

  # Runs after every rerouting hook (mangle -150, dstnat -100), so the oif
  # judged here is the final one. -20 is strictly before a kill switch module's
  # -10: with equal priorities the kernel orders chains by load order, which is
  # not fixed. Either table's reject is final.
  hookPriority = "-20";

  logRate = "6/minute burst 3 packets";

  v4Re = "[0-9]{1,3}(\\.[0-9]{1,3}){3}/[0-9]{1,2}";
  v6Re = "[0-9a-fA-F:]+/[0-9]{1,3}";
  isV4 = entry: builtins.match v4Re entry != null;
  isV6 = entry: builtins.match v6Re entry != null;
  lanV4 = builtins.filter isV4 cfg.lanAllow;
  lanV6 = builtins.filter isV6 cfg.lanAllow;
  notCidr = builtins.filter (entry: !(isV4 entry || isV6 entry)) cfg.lanAllow;

  # An empty nft set is a syntax error, so an empty list emits no rule at all.
  lanRules = lib.concatStringsSep "\n    "
    (lib.optional (lanV4 != [ ]) "ip daddr { ${lib.concatStringsSep ", " lanV4} } accept"
      ++ lib.optional (lanV6 != [ ]) "ip6 daddr { ${lib.concatStringsSep ", " lanV6} } accept");

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
        oifname { "lo", "${tailnetIface}" } accept
        ct direction reply accept
        udp sport 68 udp dport 67 accept
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
      }
    }
  '';

  # What the box runs in when ruleFile fails to load: no network the guest
  # starts, only DHCP and the replies that keep Lima's inbound ssh alive. It
  # names nothing from the options, so no local.nix can break it.
  recoveryFile = builtins.toFile "yolobox-egress-recovery.nft" ''
    table ${table}
    delete table ${table}
    table ${table} {
      chain output {
        type filter hook output priority ${hookPriority}; policy accept;
        oifname "lo" accept
        ct direction reply accept
        udp sport 68 udp dport 67 accept
        icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
        limit rate ${logRate} log prefix "yolobox-egress: recovery: " level warn
        counter reject with icmpx type admin-prohibited
      }
    }
  '';
in
{
  options.yolobox.network.lanAllow = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    example = [ "192.168.1.0/24" ];
    description = ''
      Private networks the guest may reach, e.g. a LAN package cache. Empty:
      no LAN, no host.
    '';
  };

  config = {
    assertions = [{
      assertion = notCidr == [ ];
      message = "yolobox.network.lanAllow: not a CIDR: ${lib.concatStringsSep ", " notCidr}";
    }];

    system.build.egressRules = ruleFile;
    system.build.egressRecoveryRules = recoveryFile;

    # The recovery steps in egress.md run nft by name.
    environment.systemPackages = [ pkgs.nftables ];

    # A separate nft table, not networking.nftables: that option would switch
    # the firewall backend, and nixos-fw stays on iptables-nft. No ExecStop:
    # stopping the unit, or a switch that restarts it, must never open the
    # box. ExecStartPre loads the recovery table first, so when ExecStart
    # fails the unit is failed and the recovery table stays.
    systemd.services.yolobox-egress = {
      description = "Reject guest traffic to the host and private networks";
      wantedBy = [ "sysinit.target" ];
      before = [ "network-pre.target" "sysinit.target" ];
      unitConfig.DefaultDependencies = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStartPre = [ "${nft} --file ${recoveryFile}" ];
        ExecStart = "${nft} --file ${ruleFile}";
      };
    };

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
  };
}
