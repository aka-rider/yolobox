{ config, lib, pkgs, ... }:
let
  herdReport = pkgs.writeShellApplication {
    name = "yolobox-herd-report";
    runtimeInputs = [ pkgs.herdr ];
    # Ported verbatim from herd-report.sh at 4c154fc. CRITICAL: reports use
    # the `yolobox:<agent>` source, NOT `herdr:<agent>` — the server reserves
    # herdr:* sources for its own screen/session detection of a real agent
    # process and clears them for a boxed agent (whose foreground process is
    # not the agent itself), which is exactly why a boxed agent would
    # otherwise show as unknown. Best-effort and ALWAYS exit 0: a reporting
    # failure must never disturb the agent — every guard below is an early
    # `exit 0`, never a bare failing command, so writeShellApplication's
    # `set -euo pipefail` cannot turn a guard miss into a nonzero exit.
    text = ''
      state="''${1:-}"
      agent="''${2:-}"

      [ "''${YOLOBOX_HERD:-}" = "1" ]     || exit 0
      [ -n "''${HERDR_PANE_ID:-}" ]       || exit 0
      [ -S "''${HERDR_SOCKET_PATH:-}" ]   || exit 0
      command -v herdr >/dev/null 2>&1    || exit 0
      [ -n "''${agent}" ]                 || exit 0

      # A hook harness (e.g. Claude Code) may pipe the event JSON on stdin;
      # key off the state argument instead, so drain it.
      cat >/dev/null 2>&1 || true

      case "''${state}" in
          release)
              herdr pane release-agent "''${HERDR_PANE_ID}" \
                  --source "yolobox:''${agent}" --agent "''${agent}" >/dev/null 2>&1 || true
              ;;
          working|idle|blocked)
              herdr pane report-agent "''${HERDR_PANE_ID}" \
                  --source "yolobox:''${agent}" --agent "''${agent}" --state "''${state}" >/dev/null 2>&1 || true
              ;;
      esac

      exit 0
    '';
  };

  piMcpAdapter = pkgs.callPackage ./pkgs/pi-mcp-adapter.nix { };

  piExtension = ''
    // yolobox pi herd-report extension — plain JavaScript. pi's extension
    // loader resolves TypeScript via jiti, but Node's `node --check` (used
    // to validate this file at build time) cannot parse TS syntax, so this
    // stays plain JS.
    //
    // Ported from tools/23-pi.sh at 4c154fc, reporting under source
    // yolobox:pi via yolobox-herd-report (see nix/herd-report.nix), not
    // herdr:pi — the herdr server reserves herdr:* sources for its own
    // screen/session detection and clears them for a boxed agent.
    //
    // Two subtleties ported from the reference (do not "simplify" these away):
    //   - session_shutdown also fires on /reload, /new, /resume and /fork,
    //     not just on quit. Releasing the pane on any of those would drop a
    //     live agent out of the herd list. Only reason === "quit" releases.
    //   - agent_end is debounced so a multi-step turn (many agent_start/
    //     agent_end pairs in quick succession) doesn't flicker the pane
    //     between working and idle.

    const { execFile } = require("node:child_process");

    const HERD_REPORT = "${herdReport}/bin/yolobox-herd-report";
    const AGENT_END_DEBOUNCE_MS = 250;

    function report(state) {
        execFile(HERD_REPORT, [state, "pi"], () => {});
    }

    module.exports = function (pi) {
        let agentEndTimer;

        function cancelAgentEndTimer() {
            if (agentEndTimer) {
                clearTimeout(agentEndTimer);
                agentEndTimer = undefined;
            }
        }

        pi.on("session_start", () => {
            cancelAgentEndTimer();
            report("idle");
        });

        pi.on("agent_start", () => {
            cancelAgentEndTimer();
            report("working");
        });

        pi.on("agent_end", () => {
            cancelAgentEndTimer();
            agentEndTimer = setTimeout(() => {
                agentEndTimer = undefined;
                report("idle");
            }, AGENT_END_DEBOUNCE_MS);
            if (typeof agentEndTimer.unref === "function") {
                agentEndTimer.unref();
            }
        });

        pi.on("session_shutdown", (event) => {
            if (event && event.reason === "quit") {
                cancelAgentEndTimer();
                report("release");
            }
        });
    };
  '';

  piSettings = {
    packages = [ "${piMcpAdapter}/lib/node_modules/pi-mcp-adapter" ];
    extensions = [ "/etc/yolobox/pi/yolobox-agent-state.js" ];
  };

  hookCmd = state: {
    hooks = [{ type = "command"; command = "${herdReport}/bin/yolobox-herd-report ${state} claude"; }];
  };
in
{
  config = {
    environment.systemPackages = [ herdReport ];

    # Hook map ported verbatim from tools/20-claude-code.sh:38-65 at 4c154fc
    # (SessionStart/UserPromptSubmit/PreToolUse/PermissionRequest/Stop/
    # SessionEnd), calling yolobox-herd-report instead of the old
    # /opt/yolobox/herd-report.sh path.
    environment.etc."claude-code/managed-settings.json".text = builtins.toJSON {
      hooks = {
        SessionStart = [ (hookCmd "idle") ];
        UserPromptSubmit = [ (hookCmd "working") ];
        PreToolUse = [ ((hookCmd "working") // { matcher = "*"; }) ];
        PermissionRequest = [ (hookCmd "blocked") ];
        Stop = [ (hookCmd "idle") ];
        SessionEnd = [ (hookCmd "release") ];
      };
    };

    environment.etc."yolobox/pi/yolobox-agent-state.js".text = piExtension;
    environment.etc."yolobox/pi/settings.json".text = builtins.toJSON piSettings;

    # pi may rewrite ~/.pi/agent/settings.json (e.g. via /extensions), so
    # this is copy-if-absent (Gotcha 14), not a symlink.
    systemd.tmpfiles.rules = [
      "C /home/xiii.guest/.pi/agent/settings.json 0644 xiii users - /etc/yolobox/pi/settings.json"
    ];
  };
}
