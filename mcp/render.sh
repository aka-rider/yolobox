#!/usr/bin/env bash
# Merge every mcp/*.json fragment (one JSON object per MCP server, canonical
# Claude mcpServers-entry shape) into every harness's native MCP config. Run
# at build time as root. Fails the build on any invalid fragment.
#
# Generic: a harness's config path, jq transform and file mode come from its
# owning tools/NN-*.sh module's TOOL_MCP_PATH / TOOL_MCP_JQ / TOOL_MCP_MODE --
# this script names no harness itself. Per-harness schema rationale (the
# crush-executes-$(...)-at-load hazard, opencode's command/environment shape)
# now lives in tools/README.md's harness facet section and inline in each
# owning module.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=tools/lib.sh
source /opt/yolobox/tools/lib.sh

mkdir -p /opt/yolobox/mcp /opt/yolobox/manifest

# Validate every fragment first, so a bad one fails loudly rather than
# silently dropping a server via `jq -s`.
for f in "${here}"/*.json; do
    jq -e . "${f}" >/dev/null
done

merged="$(jq -s 'reduce .[] as $x ({}; . * $x)' "${here}"/*.json)"
# The canonical merge, for mcp/smoke.sh's "every rendered config contains
# every server" check -- it runs unprivileged and cannot source modules, so
# this is the one further generated artifact it reads.
printf '%s' "${merged}" > /opt/yolobox/manifest/canonical-mcp.json

: > /opt/yolobox/manifest/mcp-configs

declare -A mcp_path_owner=()

render_cb() {
    local m="$1"
    [ -n "${TOOL_MCP_PATH+x}" ] || return 0
    : "${TOOL_MCP_JQ:?${m} declares TOOL_MCP_PATH without TOOL_MCP_JQ}"

    local path="${TOOL_MCP_PATH}" mode="${TOOL_MCP_MODE:-0444}"

    if [ -n "${mcp_path_owner[${path}]+x}" ]; then
        echo "mcp/render.sh: TOOL_MCP_PATH '${path}' declared by both ${mcp_path_owner[${path}]} and ${m}" >&2
        exit 1
    fi
    mcp_path_owner["${path}"]="${m}"

    mkdir -p "$(dirname "${path}")"
    printf '%s' "${merged}" | jq "${TOOL_MCP_JQ}" > "${path}"
    jq -e . "${path}" >/dev/null

    # Reject an empty-string (or null) top-level key outright. This is the
    # generic invariant that defeats a double-quoted TOOL_MCP_JQ silently
    # expanding "$schema" to "": bash corrupts the FILTER STRING
    # itself before jq ever runs, so by the time this script sees
    # TOOL_MCP_JQ the damage is already done -- the rendered output's own
    # shape is the only reliable place left to catch it.
    if ! jq -e 'keys_unsorted | all(. != "" and . != null)' "${path}" >/dev/null; then
        echo "mcp/render.sh: ${m} rendered ${path} with an empty top-level key -- check that its TOOL_MCP_JQ is SINGLE-quoted" >&2
        exit 1
    fi

    chmod "${mode}" "${path}"

    # Classify by testing for presence of each known vocabulary's key,
    # never by key ORDER: `keys_unsorted[0]` recorded whichever key a
    # filter happened to emit first, so a filter shaped
    # `{"mcp": ..., "$schema": ...}` would have mis-recorded "mcp" and
    # failed mcp/smoke.sh's vocabulary check on a perfectly valid config.
    local expected_key
    if jq -e 'has("mcpServers")' "${path}" >/dev/null; then
        expected_key="mcpServers"
    elif jq -e 'has("$schema")' "${path}" >/dev/null; then
        expected_key="\$schema"
    else
        echo "mcp/render.sh: ${m} rendered ${path} with neither a mcpServers nor a \$schema top-level key" >&2
        exit 1
    fi
    printf '%s\t%s\n' "${path}" "${expected_key}" >> /opt/yolobox/manifest/mcp-configs
}
yb_each_module render_cb

chmod 0555 /opt/yolobox/mcp
chmod 0444 /opt/yolobox/manifest/mcp-configs /opt/yolobox/manifest/canonical-mcp.json
