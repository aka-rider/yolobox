# What every box module shares, as the one module argument `yolobox`, and the
# facts the box renders for its shell scripts (base.nix writes them to
# /etc/yolobox/facts.sh). `facts` is facts.json at the repo root, the one
# spelling of every literal yo, the box and the scripts share.
{ lib, pkgs, facts, ... }:
let
  display = ":0";

  # One fact as the words of its YOLOBOX_<KEY> line: a string or an integer as
  # itself, a list as its words, a map of strings as key:value words. Any
  # other shape has no rendering the shell scripts read, and is refused.
  renderFact = key: value:
    if builtins.isString value || builtins.isInt value then toString value
    else if builtins.isList value && builtins.all builtins.isString value then lib.concatStringsSep " " value
    else if builtins.isAttrs value && builtins.all builtins.isString (lib.attrValues value) then
      lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "${k}:${v}") value)
    else throw "facts.json: ${key} is ${builtins.typeOf value}, which has no YOLOBOX_${lib.toUpper key} rendering (string, integer, list of strings, or map of strings)";
in
{
  options.yolobox.facts = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = { };
    description = "Facts of the box, one KEY=value line each, shell-quoted into /etc/yolobox/facts.sh.";
  };

  config._module.args.yolobox = {
    # The agent's environment, applied identically to every agent process: the
    # herdr server and its panes, and every agent login shell. Most of them
    # start without a login shell, so a variable set only in shell init would
    # miss them. The operator's shells carry none of it.
    agentEnv = {
      DISPLAY = display;
      SSH_AUTH_SOCK = facts.agent_auth_sock;
      AWS_CONFIG_FILE = "${facts.agent_home}/.config/yolobox/aws-config";
      # pi emits OSC 52 only with SSH_* set or no DISPLAY; herdr panes have
      # neither, so opt in (pi >= 1.2.0).
      PI_CLIPBOARD_OSC52 = "1";
    };
    # The X display the agent's units and shells draw on.
    inherit display;

    # A unit of the agent's user manager. Without ConditionUser the operator's
    # user manager would run it too.
    agentUnit = unit: lib.recursiveUpdate { unitConfig.ConditionUser = facts.agent_user; } unit;

    # tmpfiles rules that link files into the agent's home without home-manager.
    # The "d" rules own every parent directory as the agent first, or tmpfiles
    # creates them root-owned and refuses the leaf as an unsafe path
    # transition. The links belong to the agent too: with "-" tmpfiles makes
    # them root's. The rules also run on `switch` (systemd-tmpfiles-resetup),
    # force-replacing the "L+" links.
    agentHomeTmpfiles = { dirs, links ? [ ] }:
      map (d: "d ${facts.agent_home}/${d} 0755 ${facts.agent_user} users -") dirs
      ++ map (l: "L+ ${facts.agent_home}/${l.path} - ${facts.agent_user} users - ${l.argument}") links;

    # One herdr build for the server unit and the harness installer.
    herdrPkg = pkgs.callPackage ../pkgs/herdr-bin.nix { };
  };

  # Every fact, as YOLOBOX_<KEY>=value for the shell scripts; a list is its
  # words space-separated. hardening.nix adds the values it computes from the
  # evaluated config.
  config.yolobox.facts = lib.mapAttrs'
    (key: value: lib.nameValuePair
      ("YOLOBOX_" + lib.toUpper key)
      (renderFact key value))
    facts;
}
