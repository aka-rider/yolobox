{ config, pkgs, ... }:
let
  tailscale = config.services.tailscale.package;
in
{
  services.tailscale = {
    enable = true;
    openFirewall = true;
    extraSetFlags = [ "--accept-dns=false" "--hostname=${config.networking.hostName}" ];
  };

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
