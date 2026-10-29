{ config, pkgs, ... }:
{
  # Every port, UDP and IPv6 included, is open to the tailnet; the tailnet ACL
  # is the access control. base.nix opens only 22 on enp0s1, for lima's
  # hostagent.
  networking.firewall.trustedInterfaces = [ "tailscale0" ];

  services.tailscale = {
    enable = true;
    openFirewall = true;
    # TS_NO_LOGS_NO_SUPPORT=true: no log uploads to log.tailscale.com, and with
    # them no Tailscale support for this node.
    disableUpstreamLogging = true;
    # --ssh=false: tailscaled must not run its own ssh server on the tailnet.
    # --netfilter-mode=off: nixos-fw owns the rules; tailscaled must not insert
    # its own (its default puts "-A INPUT -j ts-input" ahead of nixos-fw).
    # --accept-dns=false: MagicDNS must not take over the guest's resolver.
    # --exit-node-allow-lan-access: expected to keep 192.168.5.0/24 (Lima's ssh
    # replies) out of tailscale's table 52 once an exit node is selected. Not
    # yet verified on a live box; see nix/tailscale.md. egress.nix still rejects
    # the private ranges, except the networks in lanAllow.
    extraSetFlags = [ "--accept-dns=false" "--ssh=false" "--netfilter-mode=off" "--exit-node-allow-lan-access" "--hostname=${config.networking.hostName}" ];
  };

  # tailscaled makes its socket 0666 itself, so the directory mode is what keeps
  # the agent, which is not in wheel, off it. The operator is in wheel and keeps
  # `tailscale status` working. The chgrp must be ExecStartPost: systemd re-applies
  # the unit's owner (root:root) to /run/tailscale for every command it spawns, so
  # a chgrp in ExecStartPre is undone by the next gate and by ExecStart. A restart
  # re-runs the whole sequence, ExecStartPost included. See nix/tailscale.md.
  systemd.services.tailscaled.serviceConfig.RuntimeDirectoryMode = "0750";
  systemd.services.tailscaled.serviceConfig.ExecStartPost = [ "+${pkgs.coreutils}/bin/chgrp wheel /run/tailscale" ];

  # Its NAT-PMP, PCP and SSDP go only to the host and the LAN, which
  # nix/egress.nix rejects. Peers fall back to public endpoints, then DERP.
  systemd.services.tailscaled.environment.TS_DISABLE_PORTMAPPER = "1";

  # tailscaled logs one "new contact ... via=derp" line for every disco
  # call-me-maybe a peer sends while the box has no direct path to it, which
  # is every few seconds for as long as the path stays relayed, and the box
  # sits behind lima's NAT, so it often does. The line carries nothing that
  # `tailscale status` does not, and tailscaled has no switch for it. The
  # direct-path lines and "now using" stay. See nix/tailscale.md.
  systemd.services.tailscaled.serviceConfig.LogFilterPatterns =
    "~magicsock: new contact: peer=[^ ]+ usec=[0-9]+ cached=[a-z]+ via=derp";

  # nixpkgs declares tailscaled-set as a bare oneshot, which is "inactive" once
  # it has run. switch-to-configuration restarts only units that are active, so
  # a changed extraSetFlags would apply at the next boot and not on the switch.
  # RemainAfterExit keeps it "active (exited)", so a changed unit is restarted.
  systemd.services.tailscaled-set.serviceConfig.RemainAfterExit = true;
}
