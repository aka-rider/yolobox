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
    extraSetFlags = [ "--accept-dns=false" "--ssh=false" "--netfilter-mode=off" "--hostname=${config.networking.hostName}" ];
  };

  # tailscaled makes its socket 0666 itself, so the directory mode is what keeps
  # the agent, which is not in wheel, off it. The operator is in wheel and keeps
  # `tailscale status` working. The list merges with other modules' ExecStartPre.
  # See nix/tailscale.md.
  systemd.services.tailscaled.serviceConfig.RuntimeDirectoryMode = "0750";
  systemd.services.tailscaled.serviceConfig.ExecStartPre = [ "+${pkgs.coreutils}/bin/chgrp wheel /run/tailscale" ];

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
