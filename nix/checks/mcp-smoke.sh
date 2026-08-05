#!/usr/bin/env bash
# Runtime smoke test for every MCP config nix/mcp.nix rendered. Nix already
# guarantees each config is well-formed JSON (it wrote it via
# builtins.toJSON), so unlike the v1 bash smoke test this skips structural
# checks and goes straight to the protocol check: read
# /etc/yolobox/mcp/manifest.json, extract each server's command/args per its
# harness format, and speak one JSON-RPC `initialize` request to prove the
# binary resolves and actually answers MCP.
set -uo pipefail

MANIFEST=/etc/yolobox/mcp/manifest.json
REQUEST='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"yolobox-smoke","version":"1"}}}'
ARGSEP=$'\001'

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

    # Reshape every format to a common "name<TAB>command<TAB>args" stream;
    # args are joined on 0x01 (a byte no server's own arg will contain).
    case "${format}" in
        mcpServers) servers_filter='.mcpServers | to_entries[] | [.key, .value.command, ((.value.args // []) | join("\u0001"))] | join("\t")' ;;
        crush)      servers_filter='.mcp | to_entries[] | [.key, .value.command, ((.value.args // []) | join("\u0001"))] | join("\t")' ;;
        opencode)   servers_filter='.mcp | to_entries[] | [.key, (.value.command[0]), ((.value.command[1:]) | join("\u0001"))] | join("\t")' ;;
        *)
            echo "mcp-smoke: unrecognised format '${format}' for ${cfg_path}" >&2
            failed="${failed} ${cfg_path}"
            continue
            ;;
    esac

    while IFS=$'\t' read -r name cmd argstr; do
        [ -n "${name}" ] || continue
        printf '  %-16s (%s)' "${name}" "${cfg_path}"

        args=()
        if [ -n "${argstr}" ]; then
            IFS="${ARGSEP}" read -r -a args <<< "${argstr}"
        fi

        out="$(printf '%s\n' "${REQUEST}" | timeout 60 "${cmd}" "${args[@]}" 2>/dev/null | head -n1 || true)"
        server_info="$(printf '%s' "${out}" | jq -r 'select(.id==1) | .result.serverInfo.name // empty' 2>/dev/null || true)"

        if [ -n "${server_info}" ]; then
            echo "  OK ${server_info}"
        else
            echo "  FAIL"
            failed="${failed} ${name}@${cfg_path}"
        fi
    done < <(jq -r "${servers_filter}" "${cfg_path}")
done < <(jq -r '.[] | [.path, .format] | join("\t")' "${MANIFEST}")

if [ -n "${failed}" ]; then
    echo "=== MCP SMOKE FAILED:${failed} ===" >&2
    exit 1
fi
echo "=== all MCP configs validated and every server responded to initialize ==="
