{ config, lib, pkgs, ... }:
let
  cfg = config.yolobox.network;
  nft = "${pkgs.nftables}/bin/nft";

  table = "inet yolobox-killswitch";
  tailnetIface = "tailscale0";

  # Lima's gateway. Packets to it leave by the Lima NAT, so DNS sent there would
  # be a direct path to the Mac's network and the Mac's resolver.
  limaGateway = "192.168.5.2";
  limaSubnet = "192.168.5.0/24";

  # tailscaled marks its own packets (control, DERP, UDP 41641) 0x80000 under
  # the 0xff0000 mask. They must leave by the Lima interface because they are
  # what builds the tunnel everything else travels in.
  underlayMark = "0x00080000";
  underlayMask = "0x00ff0000";

  # Runs after every rerouting hook (mangle -150, dstnat -100), so the oif and
  # mark judged here are the final ones, and before the filter hooks at 0.
  hookPriority = "-10";

  logRate = "6/minute burst 3 packets";

  # One atomic nft transaction: `table`, `delete table` and `table {..}` in one
  # file, so a reload swaps the rules with no instant at which the table is
  # missing. builtins.toFile, not pkgs.writeText: the text names no store path,
  # and toFile writes the file at eval time, so CI can `nft -c` it on any runner
  # without building anything (.github/workflows/verify.yml).
  ruleFile = builtins.toFile "yolobox-killswitch.nft" ''
    table ${table}
    delete table ${table}
    table ${table} {
      chain output {
        type filter hook output priority ${hookPriority}; policy accept;
        oifname { "lo", "${tailnetIface}" } accept
        # Nothing of tailscaled's that builds the tunnel is addressed to the Lima
        # link. Its port mapper is: NAT-PMP and PCP to the gateway, which is the
        # Mac, and SSDP multicast.
        meta mark & ${underlayMask} == ${underlayMark} ip daddr { ${limaSubnet}, 224.0.0.0/4 } counter reject with icmpx type admin-prohibited
        meta mark & ${underlayMask} == ${underlayMark} ip6 daddr { fe80::/10, ff00::/8 } counter reject with icmpx type admin-prohibited
        meta mark & ${underlayMask} == ${underlayMark} counter accept
        ct direction reply accept
        udp sport 68 udp dport 67 accept
        icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
        # MLD reports go to link-local groups; ff02::/16 stops the accept passing one off the link.
        icmpv6 type { mld-listener-report, mld2-listener-report } ip6 daddr ff02::/16 accept
        limit rate ${logRate} log prefix "yolobox-killswitch: direct egress blocked: " level warn
        counter reject with icmpx type admin-prohibited
      }
      chain forward {
        type filter hook forward priority ${hookPriority}; policy accept;
        oifname { "lo", "${tailnetIface}" } accept
        counter drop
      }
    }
  '';

  # Egress needs an address, and dhcpcd gives the box one, so dhcpcd and
  # tailscaled refuse to start while the table is absent. Wants= starts the kill
  # switch in the same transaction, so a switch that restarts dhcpcd before it
  # has started the kill switch still finds the table, and After= orders it
  # first. ExecStartPre is the gate, and it holds even when the kill switch
  # failed. Not Requires=: that would restart them,
  # dropping the lease and the operator's ssh, whenever a switch restarts the
  # kill switch. `+` runs the check as root, outside dhcpcd's sandbox.
  needsTable = {
    wants = [ "yolobox-killswitch.service" ];
    after = [ "yolobox-killswitch.service" ];
    serviceConfig.ExecStartPre = [ "+${nft} list table ${table}" ];
  };
in
{
  options.yolobox.network.exitNode = lib.mkOption {
    type = lib.types.nullOr (lib.types.strMatching "[a-z0-9][a-z0-9-]*");
    default = null;
    example = "my-exit-node";
    description = ''
      The tailnet host name (as `tailscale status` shows it) of the node the box
      sends all egress through. Setting it arms the kill switch: the nft table,
      the exit-node selection in nix/tailscale.nix, the nameserver assertion and
      /etc/yolobox/network.json. Null, the default, adds none of that, and the
      box behaves as one that has no kill switch.
    '';
  };

  # Opt-in: with no exit node named the module adds nothing, so a box that
  # never opted in is the box without a kill switch.
  config = lib.mkIf (cfg.exitNode != null) {
    assertions = [{
      assertion = config.networking.nameservers != [ ] && !(builtins.elem limaGateway config.networking.nameservers);
      message = ''
        networking.nameservers must name a resolver reachable over the tailnet, and not ${limaGateway}.
        The kill switch rejects ${limaGateway}, so the box would have no DNS. Set it per box in
        /etc/yolobox/local.nix, for example:
          networking.nameservers = [ "100.101.102.103" ];
      '';
    }];

    # /etc/yolobox/network.json is the desired state that `yolobox-guest
    # egress-status` compares with what is actually running.
    environment.etc."yolobox/network.json".text = builtins.toJSON { exitNode = cfg.exitNode; };

    system.build.killswitchRules = ruleFile;

    # The break-glass command in killswitch.md runs nft by name.
    environment.systemPackages = [ pkgs.nftables ];

    # DNS is the stock NixOS networking.nameservers, plain DNS to a resolver on
    # the tailnet, which WireGuard already encrypts. dhcpcd must not add the
    # lease's resolver (the Lima gateway).
    networking.dhcpcd.extraConfig = "nohook resolv.conf";

    # A separate nft table, not networking.nftables: that option would switch
    # the firewall backend, and nixos-fw stays on iptables-nft. No ExecStop:
    # stopping the unit, or a switch that restarts it, must never open the
    # box. The restart is safe because nft -f swaps the table atomically.
    systemd.services.yolobox-killswitch = {
      description = "Reject egress that bypasses the tailnet";
      wantedBy = [ "sysinit.target" ];
      before = [ "network-pre.target" "sysinit.target" ];
      unitConfig.DefaultDependencies = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${nft} --file ${ruleFile}";
      };
    };

    systemd.services.dhcpcd = needsTable;
    systemd.services.tailscaled = needsTable;
  };
}
