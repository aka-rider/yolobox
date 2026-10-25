{ config, lib, pkgs, ... }:
let
  tailscale = config.services.tailscale.package;
  exitNode = config.yolobox.network.exitNode;

  # `tailscale wait` gives up after this long; the unit's own timeout leaves
  # room for the failure message to be printed before systemd kills it.
  readySeconds = 60;
  unitSeconds = readySeconds + 30;

  # What the serve and exit-node units share: a oneshot that runs once
  # tailscaled is up, and stays "active (exited)" so a changed unit restarts.
  tailscaleOneshot = {
    after = [ "tailscaled.service" "tailscaled-set.service" ];
    requires = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      TimeoutStartSec = "${toString unitSeconds}s";
    };
  };

  # tailscaled starts before it has loaded its state (BackendState NoState),
  # so block until the backend is Running with an IP. A node that is not
  # logged in never gets there and fails at the timeout; so does a broken
  # tailscaled, which is why the message names the state it ended in. `unit`
  # is the service to restart once the operator has fixed it.
  waitForRunning = unit: ''
    if ! ${tailscale}/bin/tailscale wait --timeout=${toString readySeconds}s; then
      state=unreadable
      if status="$(${tailscale}/bin/tailscale status --json)"; then
        state="$(${pkgs.jq}/bin/jq -r '.BackendState // "unreadable"' <<<"$status")" || state=unreadable
      fi
      echo "tailscale did not reach Running within ${toString readySeconds}s (state: $state)." >&2
      case "$state" in
        NeedsLogin)
          echo "Run: yo ssh sudo tailscale up" >&2
          echo "Then: yo ssh sudo systemctl restart ${unit}" >&2
          ;;
        NeedsMachineAuth)
          echo "The node needs approval in the tailnet admin console." >&2
          echo "Then: yo ssh sudo systemctl restart ${unit}" >&2
          ;;
        *)
          echo "Run: yo ssh journalctl -u tailscaled" >&2
          ;;
      esac
      exit 1
    fi
  '';
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
    # --accept-dns=false: MagicDNS must not take over the guest's resolver. With
    # the opt-in kill switch (nix/killswitch.nix) it would also forward queries
    # from tailscaled's own sockets, which carry the mark the kill switch exempts.
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

  systemd.services.yolobox-tailscale-serve = tailscaleOneshot // {
    description = "Expose t3 on the tailnet via tailscale serve";
    script = ''
      ${waitForRunning "yolobox-tailscale-serve"}
      ${tailscale}/bin/tailscale serve reset
      exec ${tailscale}/bin/tailscale serve --bg --http=3773 http://127.0.0.1:3773
    '';
  };

  # Opt-in with the kill switch (nix/killswitch.nix): selects the exit node it
  # leaves as the box's only way out. Absent while the option is null. It waits for Running because `tailscale set
  # --exit-node=<name>` resolves the name in the netmap, which does not exist
  # while tailscaled is still in NoState. --exit-node-allow-lan-access is
  # needed, not cosmetic: an exit node puts a default route in tailscale's
  # table 52, which rule 5270 consults before `main`, and the flag adds the
  # throw routes that send the Lima subnet back to `main`. Without it the
  # replies to the operator's ssh would be routed into the tunnel.
  systemd.services.yolobox-tailscale-exit-node = lib.mkIf (exitNode != null) (tailscaleOneshot // {
    description = "Route the box's egress through the tailnet exit node ${exitNode}";
    script = ''
      ${waitForRunning "yolobox-tailscale-exit-node"}
      if ! ${tailscale}/bin/tailscale set --exit-node=${lib.escapeShellArg exitNode} --exit-node-allow-lan-access; then
        echo "tailscale refused the exit node ${exitNode}." >&2
        echo "It must be a node of this tailnet that advertises itself as an exit node and is approved in the admin console." >&2
        echo "Run: yo ssh tailscale exit-node list" >&2
        echo "Then: yo ssh sudo systemctl restart yolobox-tailscale-exit-node" >&2
        exit 1
      fi
    '';
  });
}
