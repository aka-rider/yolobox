{ config, pkgs, ... }:
let
  tailscale = config.services.tailscale.package;
in
{
  # tailscale0 is governed by nixos-fw (see --netfilter-mode=off below), so
  # this per-interface list is the whole of what the tailnet can reach in the
  # kernel's filter: sshd. t3 does not need a rule, tailscale serve terminates
  # it in tailscaled's own netstack. base.nix opens 22 on enp0s1 for lima's
  # hostagent, and this opens it for the tailnet.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 22 ];

  services.tailscale = {
    enable = true;
    openFirewall = true;
    # TS_NO_LOGS_NO_SUPPORT=true: no log uploads to log.tailscale.com, and with
    # them no Tailscale support for this node.
    disableUpstreamLogging = true;
    # --ssh=false: tailscaled must not run its own ssh server on the tailnet.
    # --netfilter-mode=off: tailscaled installs no iptables rules. Its default
    # puts "-A INPUT -j ts-input" ahead of nixos-fw, and ts-input accepts
    # everything from tailscale0, so every port the agent binds on 0.0.0.0
    # would be reachable from the tailnet. With it off, nixos-fw governs
    # tailscale0 like any interface; tailscale serve is handled in tailscaled's
    # own netstack and does not need the kernel filter.
    extraSetFlags = [ "--accept-dns=false" "--ssh=false" "--netfilter-mode=off" "--hostname=${config.networking.hostName}" ];
  };

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
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      TimeoutStartSec = "90s";
    };
    script = ''
      # tailscaled starts before it has loaded its state (BackendState NoState),
      # so block until the backend is Running with an IP. A node that is not
      # logged in never gets there and fails at the timeout; so does a broken
      # tailscaled, which is why the message names the state it ended in.
      if ! ${tailscale}/bin/tailscale wait --timeout=60s; then
        state=unreadable
        if status="$(${tailscale}/bin/tailscale status --json)"; then
          state="$(${pkgs.jq}/bin/jq -r '.BackendState // "unreadable"' <<<"$status")" || state=unreadable
        fi
        echo "tailscale did not reach Running within 60s (state: $state)." >&2
        case "$state" in
          NeedsLogin)
            echo "Run: yo ssh sudo tailscale up" >&2
            echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-serve" >&2
            ;;
          NeedsMachineAuth)
            echo "The node needs approval in the tailnet admin console." >&2
            echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-serve" >&2
            ;;
          *)
            echo "Run: yo ssh journalctl -u tailscaled" >&2
            ;;
        esac
        exit 1
      fi
      ${tailscale}/bin/tailscale serve reset
      exec ${tailscale}/bin/tailscale serve --bg --http=3773 http://127.0.0.1:3773
    '';
  };
}
