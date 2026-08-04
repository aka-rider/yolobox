#!/usr/bin/env bash
# opencode — release tarball from GitHub straight into /usr/local/bin. This
# deliberately does NOT use https://opencode.ai/install: that script hardcodes
# INSTALL_DIR=$HOME/.opencode/bin with no override, and $HOME is the masked
# /home volume. Underneath, the installer is only curl + tar of the asset
# fetched below, plus the target selection replicated here.
TOOL_NAME=opencode
TOOL_VERSION=1.18.11

# opencode's asset targets are linux-x64 / linux-arm64, with a -baseline
# variant for x86-64 CPUs lacking AVX2 (upstream's installer picks it the same
# way, from the same flag). No /proc/cpuinfo means either arm64 or the macOS
# host running tools/build.sh — both resolve to the plain variant, and since
# every variant is published this holds regardless.
# Module-scope: yb_each_module unsets TOOL_* and re-sources each module fresh
# before every phase, so a module-scope variable can't leak stale state from
# a previous module or a previous phase.
if [ "$(yb_arch_node)" = "x64" ] && ! grep -qw avx2 /proc/cpuinfo 2>/dev/null; then
    OPENCODE_TARGET="linux-x64-baseline"
else
    OPENCODE_TARGET="linux-$(yb_arch_node)"
fi

TOOL_SOURCES=( "https://github.com/anomalyco/opencode/releases/download/v${TOOL_VERSION}/opencode-${OPENCODE_TARGET}.tar.gz" )
TOOL_SMOKE=( "opencode --version" )

TOOL_ENV=( 'OPENCODE_CONFIG=/opt/yolobox/mcp/opencode.json' )

TOOL_MCP_PATH=/opt/yolobox/mcp/opencode.json
TOOL_MCP_MODE=0444
# opencode's schema differs from the canonical (Claude) shape in three ways:
# the top-level key is "mcp", not "mcpServers"; "command" is a SINGLE argv
# array ([.command] + .args), not a command plus separate args; and the env
# key is "environment", not "env". `timeout` is deliberately omitted here —
# opencode's is in MILLISECONDS while crush's is in SECONDS, so taking each
# harness's default avoids the trap.
# shellcheck disable=SC2016
TOOL_MCP_JQ='{"$schema":"https://opencode.ai/config.json",
 "mcp": (. | map_values({
     "type": "local",
     "command": ([.command] + (.args // [])),
     "environment": (.env // {}),
     "enabled": true
 }))}'

TOOL_REPORT=none   # opencode's plugin API has no session-end event (session.deleted
                   # is not process exit), so a pane would never be released; and
                   # plugin discovery outside $HOME is undocumented, while $HOME is
                   # a named volume the image cannot seed.

tool_install() {
    yb_fetch_tar_bin \
        "https://github.com/anomalyco/opencode/releases/download/v${TOOL_VERSION}/opencode-${OPENCODE_TARGET}.tar.gz" \
        opencode
}
