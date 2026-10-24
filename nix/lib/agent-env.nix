# The agent's environment, declared once. `env` is applied identically to every
# agent process:
#   - the herdr server and its panes (nix/herd-server.nix),
#   - t3 (the drop-in in nix/t3.nix),
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
    SSH_AUTH_SOCK = "/run/yolobox/op-agent.sock";
    AWS_CONFIG_FILE = "${agentHome}/.config/yolobox/aws-config";
    # The dotfiles set GOPATH only in interactive zsh; every other process
    # fell back to Go's default ~/go, splitting the module cache in two.
    GOPATH = "${agentHome}/Developer/go";
    # pi emits OSC 52 only with SSH_* set or no DISPLAY; herdr panes have
    # neither, so opt in (PI_CLIPBOARD_OSC52, pi >= 1.2.0). Replaces the old
    # xclip/xsel OSC 52 shim.
    PI_CLIPBOARD_OSC52 = "1";
    # The module's own DIRENV_CONFIG=/etc/direnv carries no whitelist, so
    # root and the operator never auto-trust a project .envrc. The agent
    # alone points at this directory (nix/base.nix renders it), whose
    # direnv.toml whitelists the agent's home. A variable here, not only in
    # the login-shell init, because zed's `direnv export json` and t3 run
    # without a login shell.
    DIRENV_CONFIG = "/etc/direnv-agent";
  };
  awsBrokerSock = "/run/yolobox/aws-broker.sock";
}
