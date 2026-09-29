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
    };
    script = ''
      state="$(${tailscale}/bin/tailscale status --json | ${pkgs.jq}/bin/jq -r .BackendState)"
      if [ "$state" != "Running" ]; then
        echo "tailscale is not logged in (state: $state)." >&2
        echo "Run: yo ssh sudo tailscale up" >&2
        echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-serve" >&2
        exit 1
      fi
      ${tailscale}/bin/tailscale serve reset
      exec ${tailscale}/bin/tailscale serve --bg --http=3773 http://127.0.0.1:3773
    '';
  };
}
