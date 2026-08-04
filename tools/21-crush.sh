#!/usr/bin/env bash
# Crush — installed via apt from the Charm repo.
TOOL_NAME=crush
TOOL_APT=(crush)
TOOL_SOURCES=(
    "https://repo.charm.sh/apt/gpg.key"
    "https://repo.charm.sh/apt/Release"
)
TOOL_SMOKE=( "crush --version" )

# CRUSH_GLOBAL_CONFIG is documented as a *directory* override in some crush
# docs, but pointing it at the rendered file (not a directory) is what
# currently ships and passes the build gates.
TOOL_ENV=( 'CRUSH_GLOBAL_CONFIG=/opt/yolobox/mcp/crush.json' )

TOOL_MCP_PATH=/opt/yolobox/mcp/crush.json
TOOL_MCP_MODE=0444
# crush's stdio shape (type/command/args/env) matches ours 1:1. Omit
# `timeout` — crush's is in SECONDS, opencode's in MILLISECONDS; taking
# defaults avoids the trap. crush treats its config as trusted code and
# executes $(...) at load — never emit any.
TOOL_MCP_JQ='{"$schema":"https://charm.land/crush.json","mcp": .}'

# PreToolUse is crush's only hook event — no session, prompt, idle or
# permission events. Upstream charmbracelet/crush#2707 requests full
# lifecycle hooks; unshipped as of this writing. This is a declared
# capability, not an omission: crush can only ever signal "working" and
# could never clear back to idle, which would leave a permanently stale
# pane — worse than reporting nothing.
TOOL_REPORT=none

tool_apt_repo() {
    yb_apt_repo charm \
        "https://repo.charm.sh/apt/gpg.key" \
        "deb [signed-by=/etc/apt/keyrings/charm.gpg] https://repo.charm.sh/apt/ * *"
}
