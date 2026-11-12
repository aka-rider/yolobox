{ pkgs, facts, ... }:
let
  nft = "${pkgs.nftables}/bin/nft";
  unit = "${facts.egress_unit}.service";
in
{
  # The table the box runs under, as checked in. A separate nft table, not
  # networking.nftables: that option would switch the firewall backend, and
  # nixos-fw stays on iptables-nft. No ExecStop: stopping the unit must never
  # open the box.
  systemd.services.${facts.egress_unit} =
    let load = "${nft} --file ${./egress/yolobox-egress.nft}"; in {
    description = "Reject guest traffic to the host and private networks";
    wantedBy = [ "sysinit.target" ];
    before = [ "network-pre.target" "sysinit.target" ];
    unitConfig.DefaultDependencies = false;
    # A switch reloads, never restarts: a restart would restart every unit that
    # requires this one; the file swaps the table in one transaction.
    reloadIfChanged = true;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = load;
      ExecReload = load;
    };
  };

  # Fail closed by dependency: without the table there is no address, so
  # nothing leaves the box.
  systemd.services.dhcpcd = {
    requires = [ unit ];
    after = [ unit ];
  };

  # The table rejects ff00::/8, so DHCPv6 to ff02::1:2 never leaves and dhcpcd
  # would log "sendmsg: Operation not permitted" for every try; SLAAC suffices.
  networking.dhcpcd.extraConfig = "nodhcp6";

  # The operator inspects the table by name, and the libvirt acceptance check
  # deletes and restores it from inside the guest.
  environment.systemPackages = [ pkgs.nftables ];
}
