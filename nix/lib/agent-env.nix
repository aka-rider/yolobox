{ agentHome }:
{
  env = {
    SSH_AUTH_SOCK = "/run/yolobox/op-agent.sock";
    AWS_CONFIG_FILE = "${agentHome}/.config/yolobox/aws-config";
  };
  awsBrokerSock = "/run/yolobox/aws-broker.sock";
}
