{ config, lib, pkgs, username, ... }:
let
  homeDir = config.users.users.${username}.home;
  homeTmpfiles = import ./lib/home-tmpfiles.nix;
  claudeHooksFile = import ./lib/claude-hooks-file.nix;
  herdrPkg = import ./lib/herdr-pkg.nix { inherit pkgs; cfg = config.yolobox.harness.herdr; };

  herdReport = pkgs.writeShellApplication {
    name = "yolobox-herd-report";
    runtimeInputs = [ herdrPkg pkgs.coreutils ];
    # Ported from herd-report.sh at 4c154fc, with its guards restructured for
    # set -e. CRITICAL: reports use the `yolobox:<agent>` source, NOT
    # `herdr:<agent>` — the server reserves herdr:* sources for its own
    # screen/session detection of a real agent process and clears them for a
    # boxed agent (whose foreground process is not the agent itself), which
    # is exactly why a boxed agent would otherwise show as unknown.
    #
    # Best-effort and exit 0 for every state but `start`: a reporting failure
    # must never disturb the agent — every guard below is an early `exit 0`,
    # never a bare failing command, so writeShellApplication's `set -euo
    # pipefail` cannot turn a guard miss into a nonzero exit. "No herd here"
    # (a guard miss on YOLOBOX_HERD or HERDR_PANE_ID) stays silent in EVERY
    # state — that is the ordinary `yo ssh` and editor-terminal case, and
    # turning it into output would make noise the common path.
    #
    # `start` is the one loud state, and it exists because always-exit-0 is
    # how the herd stayed broken for weeks: a rejected report was appended to
    # a guest-side log that nobody read, and which — since nothing ever
    # failed on a machine where the socket itself was missing — was usually
    # never even created. So `start` writes its diagnostic to stderr as well
    # as the log and exits 1.
    #
    # Three facts make that safe. `SessionStart` is not on the work path: it
    # runs once, before any tool call, so a nonzero exit cannot interrupt
    # anything in flight. Claude Code treats a nonzero-but-not-2 hook exit as
    # a NON-blocking error and surfaces its stderr to the user — precisely
    # the loud-but-harmless channel wanted. And the other five events stay at
    # exit 0 because `PreToolUse` fires on every single tool call: making it
    # loud would turn one broken socket into a wall of warnings for the whole
    # session.
    #
    # "Never disturb the agent" is a bound on TIME as well as exit status:
    # the herdr CLI waits indefinitely on a socket that accepts but never
    # answers (reproduced by pointing HERDR_SOCKET_PATH at any non-herdr
    # socket), and these run as blocking Claude Code hooks, so every call is
    # capped by `timeout`.
    text = ''
      state="''${1:-}"
      agent="''${2:-}"

      guest_herdr_version="${herdrPkg.version}"

      [ "''${YOLOBOX_HERD:-}" = "1" ]     || exit 0
      [ -n "''${HERDR_PANE_ID:-}" ]       || exit 0
      [ -n "''${agent}" ]                 || exit 0

      if [ "''${state}" = "start" ]; then loud=yes; else loud=no; fi

      if [ ! -S "''${HERDR_SOCKET_PATH:-}" ]; then
          if [ "''${loud}" = yes ]; then
              printf 'yolobox-herd: no socket at "%s" — the ssh -R forward never bound (the client warns at connect time); check "ls -ld /run/yolobox". This session will not appear in the herd.\n' \
                  "''${HERDR_SOCKET_PATH:-}" >&2
              exit 1
          fi
          exit 0
      fi

      # A hook harness may pipe event JSON on stdin, which is drained here
      # since the state argument is what matters. A non-TTY stdin can be a
      # pipe the parent never closes, so the drain is bounded rather than
      # trusting every caller to close it.
      [ -t 0 ] || timeout 1 cat >/dev/null 2>&1 || true

      report_timeout=5

      log_rejection() {
          [ -n "''${HOME:-}" ] || return 0
          log_dir="''${HOME}/.local/state/yolobox"
          mkdir -p "''${log_dir}" 2>/dev/null || return 0
          log_file="''${log_dir}/herd-report.log"
          printf '%s herdr=%s socket=%s %s\n' \
              "$(date -Iseconds)" "''${guest_herdr_version}" "''${HERDR_SOCKET_PATH}" "$1" \
              >> "''${log_file}" 2>/dev/null || true
          # Cap growth: PreToolUse fires on every tool call, so a broken herd
          # never stops appending. Trim to the tail rather than pulling in
          # logrotate for a best-effort hook log.
          log_max_lines=500
          log_keep_lines=250
          lines=$(wc -l < "''${log_file}" 2>/dev/null || echo 0)
          if [ "''${lines}" -gt "''${log_max_lines}" ]; then
              tail -n "''${log_keep_lines}" "''${log_file}" > "''${log_file}.tmp" 2>/dev/null \
                  && mv "''${log_file}.tmp" "''${log_file}" 2>/dev/null || true
          fi
      }

      report_failed() {
          log_rejection "$1"
          [ "''${loud}" = yes ] || return 0
          printf 'yolobox-herd: %s (guest herdr %s via %s). This session will not appear in the herd; run yolobox-herd-check for the diagnosis.\n' \
              "$1" "''${guest_herdr_version}" "''${HERDR_SOCKET_PATH}" >&2
          exit 1
      }

      case "''${state}" in
          release)
              rc=0
              out=$(timeout "''${report_timeout}" herdr pane release-agent "''${HERDR_PANE_ID}" \
                  --source "yolobox:''${agent}" --agent "''${agent}" 2>&1) || rc=$?
              # rc 124 is timeout(1)'s "killed on expiry" — the wedged-socket
              # case, where the killed CLI leaves no output to quote.
              [ "''${rc}" -eq 0 ] || report_failed "release ''${agent} (rc=''${rc}): ''${out}"
              ;;
          start|working|idle|blocked)
              # `start` is a routing token, not a herdr state: it carries the
              # loud contract above and reports the same `idle` the old
              # SessionStart hook did.
              if [ "''${state}" = "start" ]; then report_state=idle; else report_state="''${state}"; fi
              rc=0
              out=$(timeout "''${report_timeout}" herdr pane report-agent "''${HERDR_PANE_ID}" \
                  --source "yolobox:''${agent}" --agent "''${agent}" --state "''${report_state}" 2>&1) || rc=$?
              [ "''${rc}" -eq 0 ] || report_failed "''${report_state} ''${agent} (rc=''${rc}): ''${out}"
              ;;
      esac

      exit 0
    '';
  };

  # The guest half of `yo herd-check`. It lives here rather than in a checks
  # module of its own because it exercises exactly what this file renders:
  # the reporter, the hook map, and the forwarded socket they both depend on.
  herdCheck = pkgs.writeShellApplication {
    name = "yolobox-herd-check";
    runtimeInputs = [ herdrPkg herdReport pkgs.jq pkgs.coreutils pkgs.gnugrep ];
    # @HERD_HOOKS@ is substituted rather than repeated as a literal in the
    # script, so lib/claude-hooks-file.nix stays the only place that path is
    # spelled out. The placeholder is still valid bash, so the script remains
    # shellcheck-clean and `bash -n`-able on its own.
    text = builtins.replaceStrings [ "@HERD_HOOKS@" ] [ claudeHooksFile.path ]
      (builtins.readFile ./checks/herd-check.sh);
  };

  piMcpAdapter = pkgs.callPackage ./pkgs/pi-mcp-adapter.nix { };

  piExtension = ''
    // yolobox pi herd-report extension — plain JavaScript. pi's extension
    // loader resolves TypeScript via jiti, but Node's `node --check`, which
    // validates this file at build time below (see piExtensionChecked),
    // cannot parse TS syntax, so this stays plain JS.
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

    const { spawn } = require("node:child_process");

    const HERD_REPORT = "${herdReport}/bin/yolobox-herd-report";
    const AGENT_END_DEBOUNCE_MS = 250;

    // spawn, not execFile: execFile silently ignores stdio and always pipes,
    // leaving the reporter an stdin that is never closed.
    function report(state) {
        const child = spawn(HERD_REPORT, [state, "pi"], { stdio: "ignore" });
        child.on("error", () => {});
        child.unref();
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

  # node --check can only parse, not execute, so this is a cheap build-time
  # guard against a syntax error in piExtension reaching the VM.
  piExtensionChecked = pkgs.runCommand "yolobox-agent-state.js" { } ''
    cp ${pkgs.writeText "yolobox-agent-state.js" piExtension} "$out"
    ${lib.getExe' pkgs.nodejs "node"} --check "$out"
  '';

  piAdapterDir = "${piMcpAdapter}/lib/node_modules/pi-mcp-adapter";

  hookCmd = state: {
    hooks = [{ type = "command"; command = "${herdReport}/bin/yolobox-herd-report ${state} claude"; }];
  };
in
{
  config = {
    environment.systemPackages = [ herdReport herdCheck ];

    # Hook map ported from tools/20-claude-code.sh:38-65 at 4c154fc
    # (SessionStart/UserPromptSubmit/PreToolUse/PermissionRequest/Stop/
    # SessionEnd), calling yolobox-herd-report instead of the old
    # /opt/yolobox/herd-report.sh path. One deviation from the original:
    # SessionStart passes `start`, not `idle` — same report, but the state
    # that is allowed to fail loudly (see herdReport above).
    #
    # This map is delivered to claude by `--settings <this file>` (the wrapper
    # in nix/harnesses.nix), NOT by /etc/claude-code/managed-settings.json,
    # and that is not a style choice — the managed-settings channel provably
    # cannot carry it. Claude Code (verified on 2.1.220) builds its
    # `policySettings` from three tiers — remote server-fetched managed
    # settings, MDM, and /etc/claude-code/managed-settings.json — and takes
    # only the FIRST non-empty tier, with no merge:
    #
    #     let u = [remote, mdm, etcFile].filter(g => g !== null), d = u[0] ?? null;
    #
    # "Non-empty" means *any* key at all. This account's
    # ~/.claude/remote-settings.json is `{"channelsEnabled": true}`, so that
    # single unrelated flag becomes the entire policySettings object and the
    # /etc file is discarded wholesale — silently, with nothing logged and no
    # warning. Emptying that cache to `{}` made these hooks fire immediately;
    # restoring the flag stopped them again. Worse, the remote payload is
    # re-fetched and re-applied about 180ms into every session
    # ("Programmatic settings change notification for policySettings"), so
    # even a momentarily-empty cache is clobbered mid-session. The policy
    # channel is therefore unusable for this, permanently.
    #
    # `flagSettings` — what `--settings` produces — is added to the allowed
    # sources unconditionally and was verified to SURVIVE that mid-session
    # refresh. It is the only durable channel.
    environment.etc.${claudeHooksFile.etcPath}.text = builtins.toJSON {
      hooks = {
        SessionStart = [ (hookCmd "start") ];
        UserPromptSubmit = [ (hookCmd "working") ];
        PreToolUse = [ ((hookCmd "working") // { matcher = "*"; }) ];
        PermissionRequest = [ (hookCmd "blocked") ];
        Stop = [ (hookCmd "idle") ];
        SessionEnd = [ (hookCmd "release") ];
      };
    };

    environment.etc."yolobox/pi/yolobox-agent-state.js".source = piExtensionChecked;
    environment.etc."yolobox/pi/pi-mcp-adapter".source = piAdapterDir;
    environment.etc."yolobox/pi/mcp-scripting".source = "${piAdapterDir}/skills/mcp-scripting";

    # ~/.pi/agent/settings.json belongs to ~/.dotfiles, so nothing VM-specific
    # may live in it. pi auto-discovers ~/.pi/agent/extensions (a .js file, or
    # a directory whose package.json carries a "pi" manifest) and
    # ~/.pi/agent/skills, and follows symlinks, so both entries ride L+ links
    # instead. The adapter's manifest declares extensions AND skills;
    # extension auto-discovery honours only the first, hence the separate
    # link for its one skill.
    #
    # Every link targets /etc, never a store path directly: a link is
    # rewritten only when the tmpfiles services run, whereas /etc flips on
    # every `switch`, so pointing into the store risks serving the previous
    # closure to pi.
    #
    # A herdr-installed ~/.pi/agent/extensions/herdr-agent-state.ts (from
    # herdr's own integration installer) would report under source herdr:pi,
    # which the server reserves for its own screen/session detection and
    # clears for a boxed agent — knocking pi out of the herd. The `r` rule
    # below deletes that file on every boot and switch (resetup), keeping
    # yolobox:pi the only reporter.
    systemd.tmpfiles.rules = homeTmpfiles {
      home = homeDir;
      dirUser = username;
      dirs = [ ".pi" ".pi/agent" ".pi/agent/extensions" ".pi/agent/skills" ];
      links = [
        {
          path = ".pi/agent/extensions/yolobox-agent-state.js";
          argument = "/etc/yolobox/pi/yolobox-agent-state.js";
        }
        {
          path = ".pi/agent/extensions/pi-mcp-adapter";
          argument = "/etc/yolobox/pi/pi-mcp-adapter";
        }
        {
          path = ".pi/agent/skills/mcp-scripting";
          argument = "/etc/yolobox/pi/mcp-scripting";
        }
      ];
    } ++ [
      "r ${homeDir}/.pi/agent/extensions/herdr-agent-state.ts"
    ];
  };
}
