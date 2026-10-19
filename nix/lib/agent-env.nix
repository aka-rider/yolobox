{ agentHome }:
{
  env = {
    SSH_AUTH_SOCK = "/run/yolobox/op-agent.sock";
    AWS_CONFIG_FILE = "${agentHome}/.config/yolobox/aws-config";
    # The dotfiles set GOPATH only in interactive zsh; every other process
    # fell back to Go's default ~/go, splitting the module cache in two.
    GOPATH = "${agentHome}/Developer/go";
    # pi emits OSC 52 only with SSH_* set or no DISPLAY; herdr panes have
    # neither, so opt in (PI_CLIPBOARD_OSC52, pi >= 1.2.0). Replaces the old
    # xclip/xsel OSC 52 shim.
    PI_CLIPBOARD_OSC52 = "1";
  };
  awsBrokerSock = "/run/yolobox/aws-broker.sock";
}
