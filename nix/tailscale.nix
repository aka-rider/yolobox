{ config, pkgs, ... }:
let
  tailscale = config.services.tailscale.package;

  # `tailscale wait` gives up after this long. The serve unit is Type=exec, so
  # nothing else bounds it and boot never waits on it.
  readySeconds = 60;
in
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

  systemd.services.yolobox-tailscale-serve = {
    description = "Expose t3 on the tailnet via tailscale serve";
    after = [ "tailscaled.service" "tailscaled-set.service" ];
    requires = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    # Type=exec: multi-user.target does not wait on this unit, so a node that
    # never logs in delays no boot.
    serviceConfig = {
      Type = "exec";
      RemainAfterExit = true;
    };
    # tailscaled starts before it has loaded its state (BackendState NoState),
    # so block until the backend is Running with an IP. A node that is not
    # logged in never gets there: that is a choice, so the unit prints the
    # remedy and exits 0. A broken tailscaled fails at the timeout, which is
    # why the message names the state it ended in.
    script = ''
      if ! ${tailscale}/bin/tailscale wait --timeout=${toString readySeconds}s; then
        state=unreadable
        if status="$(${tailscale}/bin/tailscale status --json)"; then
          state="$(${pkgs.jq}/bin/jq -r '.BackendState // "unreadable"' <<<"$status")" || state=unreadable
        fi
        echo "tailscale did not reach Running within ${toString readySeconds}s (state: $state)." >&2
        case "$state" in
          NeedsLogin)
            echo "Run: yo ssh sudo tailscale up" >&2
            echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-serve" >&2
            exit 0
            ;;
          NeedsMachineAuth)
            echo "The node needs approval in the tailnet admin console." >&2
            echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-serve" >&2
            exit 0
            ;;
          *)
            echo "Run: yo ssh journalctl -u tailscaled" >&2
            exit 1
            ;;
        esac
      fi
      ${tailscale}/bin/tailscale serve reset
      exec ${tailscale}/bin/tailscale serve --bg --http=3773 http://127.0.0.1:3773
    '';
  };
}
