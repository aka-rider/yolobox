#!/usr/bin/env bash
# Runtime smoke test for every MCP config nix/mcp.nix rendered. Nix already
# guarantees each config is well-formed JSON (it wrote it via
# builtins.toJSON), so unlike the v1 bash smoke test this skips structural
# checks and goes straight to the protocol check: read
# /etc/yolobox/mcp/manifest.json, normalize each format to one compact JSON
# object per server ({name, command, args, env}), and speak one JSON-RPC
# `initialize` request — with the same env the harness would set — to prove
# the binary resolves and actually answers MCP.
#
# writeShellApplication prepends `set -o errexit -o nounset -o pipefail`.

MANIFEST=/etc/yolobox/mcp/manifest.json
REQUEST='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"yolobox-smoke","version":"1"}}}'
INIT_TIMEOUT_S=60

echo "=== yolobox MCP smoke ==="

if [ ! -s "${MANIFEST}" ]; then
    echo "mcp-smoke: ${MANIFEST} missing or empty" >&2
    exit 1
fi

count="$(jq 'length' "${MANIFEST}")"
if [ "${count}" -eq 0 ]; then
    echo "mcp-smoke: manifest lists zero configs" >&2
    exit 1
fi

failed=""

while IFS=$'\t' read -r cfg_path format; do
    [ -n "${cfg_path}" ] || continue

    case "${format}" in
        mcpServers) servers_filter='.mcpServers | to_entries[] | {name: .key, command: .value.command, args: (.value.args // []), env: (.value.env // {})}' ;;
        crush)      servers_filter='.mcp | to_entries[] | {name: .key, command: .value.command, args: (.value.args // []), env: (.value.env // {})}' ;;
        opencode)   servers_filter='.mcp | to_entries[] | {name: .key, command: .value.command[0], args: .value.command[1:], env: (.value.environment // {})}' ;;
        *)
            echo "mcp-smoke: unrecognised format '${format}' for ${cfg_path}" >&2
            failed="${failed} ${cfg_path}"
            continue
            ;;
    esac

    while IFS= read -r server; do
        [ -n "${server}" ] || continue

        name="$(jq -r '.name' <<< "${server}")"
        cmd="$(jq -r '.command' <<< "${server}")"
        printf '  %-16s (%s)' "${name}" "${cfg_path}"

        mapfile -t args < <(jq -r '.args[]' <<< "${server}")
        mapfile -t envs < <(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' <<< "${server}")

        out="$(printf '%s\n' "${REQUEST}" | timeout "${INIT_TIMEOUT_S}" env "${envs[@]}" "${cmd}" "${args[@]}" 2>/dev/null | head -n1 || true)"
        server_info="$(printf '%s' "${out}" | jq -r 'select(.id==1) | .result.serverInfo.name // empty' 2>/dev/null || true)"

        if [ -n "${server_info}" ]; then
            echo "  OK ${server_info}"
        else
            echo "  FAIL"
            failed="${failed} ${name}@${cfg_path}"
        fi
    done < <(jq -c "${servers_filter}" "${cfg_path}")
done < <(jq -r '.[] | [.path, .format] | join("\t")' "${MANIFEST}")

if [ -n "${failed}" ]; then
    echo "=== MCP SMOKE FAILED:${failed} ===" >&2
    exit 1
fi
echo "=== all MCP configs validated and every server responded to initialize ==="
