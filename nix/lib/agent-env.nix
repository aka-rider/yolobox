# The agent's environment, declared once. `env` is applied identically to every
# agent process:
#   - the herdr server and its panes (nix/herd-server.nix),
#   - every agent login shell (nix/base.nix, `environment.extraInit`).
# Those processes share one environment because most of them start without a
# login shell, so a variable set only in shell init would miss them.
#
# `awsBrokerSock` is not exported to anything. It names the agent-facing AWS
# broker socket, which nix/base.nix (`opProxy`) listens on and nix/guest.nix
# hands to `yolobox-guest aws-creds`.
{ agentHome }:
{
  env = {
    # The virtual display (nix/display.nix). The agent's own environment only,
    # so the operator's shells carry no DISPLAY.
    DISPLAY = ":0";
    SSH_AUTH_SOCK = "/run/yolobox/op-agent.sock";
    AWS_CONFIG_FILE = "${agentHome}/.config/yolobox/aws-config";
    # pi emits OSC 52 only with SSH_* set or no DISPLAY; herdr panes have no
    # SSH_* and do have a DISPLAY, so opt in (PI_CLIPBOARD_OSC52, pi >= 1.2.0), so no
    # xclip/xsel OSC 52 shim is needed.
    PI_CLIPBOARD_OSC52 = "1";
  };
  awsBrokerSock = "/run/yolobox/aws-broker.sock";
}
