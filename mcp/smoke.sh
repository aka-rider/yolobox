#!/usr/bin/env bash
# Build-time smoke test for every rendered MCP config. Runs UNPRIVILEGED (the
# real box user) -- it cannot source tools/*.sh modules, so everything it
# checks comes from the generated manifests mcp/render.sh wrote:
#   /opt/yolobox/manifest/mcp-configs      -- "<path>\t<expected-top-level-key>"
#   /opt/yolobox/manifest/canonical-mcp.json -- the merged fragment, for names
#
# For EVERY rendered config (not just the canonical one -- the pre-refactor
# version of this script only ever read the canonical pi config via
# .mcpServers, so a broken crush or opencode transform passed the build gate
# silently):
#   1. valid JSON
#   2. its recorded top-level key is one of the two schema vocabularies this
#      project ever emits ("mcpServers" or "$schema") -- this is what fails
#      a transform mis-keyed to something else entirely (e.g. "bogus"),
#      independent of whether the mis-keyed content still happens to be
#      well-formed JSON.
#   3. some top-level object value contains every canonical server name --
#      generic across both shapes (claude/pi keep servers directly under
#      the recorded key; crush/opencode keep them under a SECOND key, "mcp",
#      which this check finds without needing to be told its name).
#
# Empty-key hazard: a double-quoted TOOL_MCP_JQ containing "$schema" expands
# to "" INSIDE THE MODULE, before jq ever runs -- producing {"": "https://…", ...},
# valid JSON that neither `jq -e .` nor a "contains every server" check alone
# would catch. mcp/render.sh already rejects an empty top-level key at build
# time (the earliest point that corruption is visible); check 2 above is the
# second, independent line of defense against the same class of bug.
#
# Finally, speaks one JSON-RPC `initialize` request to every server listed
# under an "mcpServers"-keyed config, proving the binary resolves AND speaks
# MCP, not just that its JSON is well-formed.
set -uo pipefail

MANIFEST=/opt/yolobox/manifest/mcp-configs
CANONICAL=/opt/yolobox/manifest/canonical-mcp.json
REQUEST='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"yolobox-smoke","version":"1"}}}'

echo "=== yolobox MCP smoke ==="
failed=""

if [ ! -s "${MANIFEST}" ]; then
    echo "mcp/smoke.sh: ${MANIFEST} missing or empty" >&2
    exit 1
fi

canonical_names="$(jq -e 'keys' "${CANONICAL}")" || {
    echo "mcp/smoke.sh: ${CANONICAL} missing or invalid" >&2
    exit 1
}

echo "--- structural checks ---"
while IFS=$'\t' read -r cfg_path expected_key; do
    [ -n "${cfg_path}" ] || continue
    printf '  %-42s' "${cfg_path}"

    if ! jq -e . "${cfg_path}" >/dev/null 2>&1; then
        echo "FAIL (invalid JSON)"
        failed="${failed} ${cfg_path}"
        continue
    fi

    case "${expected_key}" in
        mcpServers|'$schema') ;;
        *)
            echo "FAIL (unrecognised top-level key '${expected_key}')"
            failed="${failed} ${cfg_path}"
            continue
            ;;
    esac

    if ! jq -e --arg k "${expected_key}" 'has($k) and (.[$k] != null) and (.[$k] != "")' \
            "${cfg_path}" >/dev/null 2>&1; then
        echo "FAIL (missing/empty key ${expected_key})"
        failed="${failed} ${cfg_path}"
        continue
    fi

    # NOTE the absence of a second `.value` inside the parentheses: after
    # `.value |` the input `.` IS already the entry's value, so `.value | keys`
    # would be a second-level lookup returning null -> `null has no keys`, and
    # jq errors out for EVERY config rather than evaluating the predicate. That
    # silently turned this into "always fail". Also parenthesise the type test,
    # since `and` binds tighter than the bare comparison reads.
    if ! jq -e --argjson names "${canonical_names}" \
            'to_entries | any(.value | (type == "object") and (($names - keys) | length == 0))' \
            "${cfg_path}" >/dev/null 2>&1; then
        echo "FAIL (missing server(s))"
        failed="${failed} ${cfg_path}"
        continue
    fi

    echo "OK"
done < "${MANIFEST}"

echo "--- protocol checks (mcpServers configs) ---"
while IFS=$'\t' read -r cfg_path expected_key; do
    [ "${expected_key}" = "mcpServers" ] || continue
    names="$(jq -r '.mcpServers | keys[]' "${cfg_path}")"
    while IFS= read -r name; do
        [ -n "${name}" ] || continue
        printf '  %-16s (%s)' "${name}" "$(basename "${cfg_path}")"

        cmd="$(jq -r --arg n "${name}" '.mcpServers[$n].command' "${cfg_path}")"
        mapfile -t args < <(jq -r --arg n "${name}" '.mcpServers[$n].args[]?' "${cfg_path}")

        out="$(printf '%s\n' "${REQUEST}" | timeout 60 "${cmd}" "${args[@]}" 2>/dev/null | head -n1 || true)"
        server_info="$(printf '%s' "${out}" | jq -r 'select(.id==1) | .result.serverInfo.name // empty' 2>/dev/null || true)"

        if [ -n "${server_info}" ]; then
            echo "  OK ${server_info}"
        else
            echo "  FAIL"
            failed="${failed} ${name}@${cfg_path}"
        fi
    done <<< "${names}"
done < "${MANIFEST}"

if [ -n "${failed}" ]; then
    echo "=== MCP SMOKE FAILED:${failed} ===" >&2
    exit 1
fi
echo "=== all MCP configs validated and every server responded to initialize ==="
