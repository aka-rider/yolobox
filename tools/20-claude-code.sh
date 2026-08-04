#!/usr/bin/env bash
# Claude Code — installed via apt from Anthropic's own repo.
TOOL_NAME=claude-code
TOOL_APT=(claude-code)
TOOL_SOURCES=(
    "https://downloads.claude.ai/keys/claude-code.asc"
    "https://downloads.claude.ai/claude-code/apt/stable/dists/stable/Release"
)
TOOL_SMOKE=( "claude --version" )

# DISABLE_AUTOUPDATER — Claude Code's own auto-updater kill switch. Verified
# (WP3 research) by grepping the `claude` binary's env-var registry: it
# appears 11 times there, adjacent to DISABLE_AUTO_COMPACT, CLAUDE_CODE_USE_VERTEX
# and DISABLE_BUG_COMMAND — and zero times in the crush, opencode or herdr
# binaries, or the pi npm tree. Unambiguously Claude Code's.
TOOL_ENV=( 'DISABLE_AUTOUPDATER=1' )

# MCP config — canonical {"mcpServers": {...}} shape, filter taken verbatim
# from mcp/render.sh. Lives under /etc, not /opt/yolobox/mcp, hence 0644
# rather than the module-default 0444.
TOOL_MCP_PATH=/etc/claude-code/managed-mcp.json
TOOL_MCP_MODE=0644
# shellcheck disable=SC2016
TOOL_MCP_JQ='{mcpServers: .}'

# Herd reporting — full support via Claude Code's shell hooks (all states).
TOOL_REPORT=full

tool_apt_repo() {
    yb_apt_repo claude-code \
        "https://downloads.claude.ai/keys/claude-code.asc" \
        "deb [signed-by=/etc/apt/keyrings/claude-code.gpg] https://downloads.claude.ai/claude-code/apt/stable stable main"
}

# Build-time, root, runs inside install-all.sh before the MCP render layer.
# Writes the managed hooks config that wires every Claude Code lifecycle
# event to herd-report.sh's two-arg form (<state> <agent>).
tool_configure() {
    mkdir -p /etc/claude-code
    jq -e . > /etc/claude-code/managed-settings.json <<'EOF'
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh idle claude" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh working claude" } ] }
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh working claude" } ] }
    ],
    "PermissionRequest": [
      { "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh blocked claude" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh idle claude" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "/opt/yolobox/herd-report.sh release claude" } ] }
    ]
  }
}
EOF
    chmod 0644 /etc/claude-code/managed-settings.json
}
