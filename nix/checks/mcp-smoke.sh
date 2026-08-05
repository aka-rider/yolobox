#!/usr/bin/env bash
# Runtime smoke test for every MCP config nix/mcp.nix rendered. Nix already
# guarantees each config is well-formed JSON (it wrote it via
# builtins.toJSON), so unlike the v1 bash smoke test this skips structural
# checks and goes straight to the protocol check: read
# /etc/yolobox/mcp/manifest.json, extract each server's command/args/env per
# its harness format, and speak one JSON-RPC `initialize` request — with the
# same env the harness would set — to prove the binary resolves and actually
# answers MCP.
#
# writeShellApplication prepends `set -o errexit -o nounset -o pipefail`.

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

    # Reshape every format to a common "name<TAB>command<TAB>args<TAB>env"
    # stream; args are joined on 0x01 (a byte no server's own arg will
    # contain), env travels along as a JSON object.
    case "${format}" in
        mcpServers) servers_filter='.mcpServers | to_entries[] | [.key, .value.command, ((.value.args // []) | join("")), (.value.env // {} | tojson)] | join("\t")' ;;
        crush)      servers_filter='.mcp | to_entries[] | [.key, .value.command, ((.value.args // []) | join("")), (.value.env // {} | tojson)] | join("\t")' ;;
        opencode)   servers_filter='.mcp | to_entries[] | [.key, (.value.command[0]), ((.value.command[1:]) | join("")), (.value.environment // {} | tojson)] | join("\t")' ;;
        *)
            echo "mcp-smoke: unrecognised format '${format}' for ${cfg_path}" >&2
            failed="${failed} ${cfg_path}"
            continue
            ;;
    esac

    while IFS=$'\t' read -r name cmd argstr envjson; do
        [ -n "${name}" ] || continue
        printf '  %-16s (%s)' "${name}" "${cfg_path}"

        args=()
        if [ -n "${argstr}" ]; then
            IFS="${ARGSEP}" read -r -a args <<< "${argstr}"
        fi

        envs=()
        while IFS= read -r kv; do
            [ -n "${kv}" ] && envs+=("${kv}")
        done < <(printf '%s' "${envjson}" | jq -r 'to_entries[] | "\(.key)=\(.value)"')

        out="$(printf '%s\n' "${REQUEST}" | timeout 60 env "${envs[@]}" "${cmd}" "${args[@]}" 2>/dev/null | head -n1 || true)"
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
