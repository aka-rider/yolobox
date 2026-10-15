{ agentHome }:
{
  env = {
    SSH_AUTH_SOCK = "/run/yolobox/op-agent.sock";
    AWS_CONFIG_FILE = "${agentHome}/.config/yolobox/aws-config";
    # The dotfiles set GOPATH only in interactive zsh; every other process
    # fell back to Go's default ~/go, splitting the module cache in two.
    GOPATH = "${agentHome}/Developer/go";
  };
  awsBrokerSock = "/run/yolobox/aws-broker.sock";
}
