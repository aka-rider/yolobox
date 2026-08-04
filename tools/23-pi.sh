#!/usr/bin/env bash
# pi — node global install with npm prefix /usr/local -> /usr/local/bin/pi.
# --ignore-scripts per the verified matrix.
#
# Also owns pi-mcp-adapter: the only way to give pi MCP support (pi has no
# native MCP subcommand). It is pi plumbing, not an MCP server, so it lives
# here rather than in the 4x band — installed as a hoisted npm tree on a
# system path, referenced by absolute path so pi never network-resolves or
# version-checks it. This used to be the separate tools/45-pi-mcp-
# adapter.sh; folded in because its /opt/yolobox/pi-packages constant would
# otherwise have to be shared with a sibling module, and module state is
# reset between modules — a shared constant can only live in
# tools/lib.sh or, more simply, in the single module that needs it.
TOOL_NAME=pi
# Module-scope only (yb_each_module unsets TOOL_* / re-sources each module in
# turn, so this is fresh every time). NOT usable inside tool_seed() — see the
# comment there.
PI_PACKAGES_DIR=/opt/yolobox/pi-packages
TOOL_SOURCES=(
    "https://registry.npmjs.org/@earendil-works%2Fpi-coding-agent"
    "https://registry.npmjs.org/pi-mcp-adapter"
)
TOOL_SMOKE=(
    "pi --version"
    "node -e \"require.resolve('pi-mcp-adapter',{paths:['${PI_PACKAGES_DIR}/node_modules']})\""
)

# --- MCP config (WP4) --------------------------------------------------------
# Filter verbatim from mcp/render.sh — pi consumes the canonical Claude shape
# via pi-mcp-adapter. Single-quoted: the filter contains { } " + //
# and newlines but no single quotes, so it survives bash 3.2 verbatim; double
# quotes would silently expand "$schema" to empty and produce valid-but-wrong
# JSON that neither `jq -e .` nor a "contains every server" check would catch.
TOOL_MCP_PATH=/opt/yolobox/mcp/mcp.json
TOOL_MCP_MODE=0444
TOOL_MCP_JQ='{mcpServers: .}'

# --- herd reporting (WP5/WP6) ------------------------------------------------
TOOL_REPORT=full

# tool_configure() — build-time, root, runs inside install-all.sh before the
# MCP RUN. Writes pi's herd-reporting extension. Plain JavaScript, NOT
# TypeScript: Node 24's `--check` cannot parse TS syntax — type-
# stripping applies to module loading only, so a `.ts` file here would fail
# validation and abort the build. pi's own loader resolves index.ts THEN
# index.js, so a `.js` extension file is fully supported.
tool_configure() {
    mkdir -p /opt/yolobox/pi
    cat > /opt/yolobox/pi/yolobox-agent-state.js <<'JS'
// yolobox pi herd-report extension — plain JavaScript. pi's extension loader
// resolves TypeScript via jiti, but Node's `node --check` (used to validate
// this file at build time) cannot parse TS syntax, so this stays plain JS.
//
// Ported from the reference herdr pi extension
// (~/.pi/agent/extensions/herdr-agent-state.ts), which speaks herdr JSON-RPC
// directly over a socket. Inside the box we instead shell out to
// herd-report.sh, consistent with the Claude Code harness, reporting under
// source yolobox:pi — the herdr server reserves herdr:* sources for its own
// screen/session detection and clears them for a containerized agent (whose
// foreground process is `docker`).
//
// Two subtleties ported from the reference (do not "simplify" these away):
//   - session_shutdown also fires on /reload, /new, /resume and /fork, not
//     just on quit. Releasing the pane on any of those would drop a live
//     agent out of the herd list. Only reason === "quit" releases.
//   - agent_end is debounced so a multi-step turn (many agent_start/agent_end
//     pairs in quick succession) doesn't flicker the pane between working and
//     idle.

const { execFile } = require("node:child_process");

const HERD_REPORT_SH = "/opt/yolobox/herd-report.sh";
const AGENT_END_DEBOUNCE_MS = 250;

function report(state) {
    execFile(HERD_REPORT_SH, [state, "pi"], () => {});
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
JS
    node --check /opt/yolobox/pi/yolobox-agent-state.js
}

tool_install() {
    local pi_mcp_adapter_version=2.15.0

    npm install -g --ignore-scripts @earendil-works/pi-coding-agent

    mkdir -p "${PI_PACKAGES_DIR}"
    printf '%s\n' '{"name":"yolobox-pi-extensions","private":true}' \
        > "${PI_PACKAGES_DIR}/package.json"
    # No --ignore-scripts: @napi-rs/keyring is a native dep whose
    # prebuilt binary needs the install scripts to run.
    npm install --prefix "${PI_PACKAGES_DIR}" \
        "pi-mcp-adapter@${pi_mcp_adapter_version}" --legacy-peer-deps
    chmod -R a+rX "${PI_PACKAGES_DIR}"
}

# --- runtime seeding (WP7) ---------------------------------------------------
# Runs at EVERY launch, from a generated file, WITHOUT this module sourced
# — so nothing below may reference a module-scope variable (TOOL_NAME,
# TOOL_MCP_PATH, PI_MCP_ADAPTER_VERSION, ...). Every path is a literal.
#
# Every other harness reads a baked system config (Claude: /etc/claude-code/
# managed-mcp.json; opencode/crush: $OPENCODE_CONFIG/$CRUSH_GLOBAL_CONFIG). pi
# reads NOTHING outside $HOME, and $HOME is the yolobox-home volume, so the
# image cannot bake it in — seed it here, idempotently, never overwriting
# user edits.
tool_seed() {
    local pi_mcp_pkg pi_agent_dir pi_extension pi_seed_tmp
    # Deliberately NOT "${PI_PACKAGES_DIR}/..." — install-all.sh extracts this
    # function body verbatim via `declare -f` and writes it into the runtime
    # seed.sh, which yolobox-init sources standalone (module NOT sourced).
    # A reference to the module-scope PI_PACKAGES_DIR would be unset there.
    # Keep this a literal; do not "DRY" it against the constant above.
    pi_mcp_pkg=/opt/yolobox/pi-packages/node_modules/pi-mcp-adapter
    pi_agent_dir="${HOME}/.pi/agent"
    pi_extension=/opt/yolobox/pi/yolobox-agent-state.js

    [ -d "${pi_mcp_pkg}" ] || return 0

    mkdir -p "${pi_agent_dir}" "${HOME}/.config/mcp" 2>/dev/null

    # pi-mcp-adapter's highest-precedence config path. A SYMLINK, so it
    # tracks the image across rebuilds instead of going stale like a copy
    # would.
    if [ ! -e "${HOME}/.config/mcp/mcp.json" ]; then
        ln -s /opt/yolobox/mcp/mcp.json "${HOME}/.config/mcp/mcp.json" 2>/dev/null
    fi

    if [ ! -f "${pi_agent_dir}/settings.json" ]; then
        printf '{"packages":["%s"],"extensions":["%s"]}\n' \
            "${pi_mcp_pkg}" "${pi_extension}" \
            > "${pi_agent_dir}/settings.json" 2>/dev/null
    elif command -v jq >/dev/null 2>&1; then
        if pi_seed_tmp="$(jq --arg p "${pi_mcp_pkg}" --arg e "${pi_extension}" \
                '.packages = ((.packages // []) as $x | if ($x | index($p)) then $x else $x + [$p] end)
                 | .extensions = ((.extensions // []) as $x | if ($x | index($e)) then $x else $x + [$e] end)' \
                "${pi_agent_dir}/settings.json" 2>/dev/null)"; then
            printf '%s\n' "${pi_seed_tmp}" > "${pi_agent_dir}/settings.json" 2>/dev/null
        fi
    fi
}
