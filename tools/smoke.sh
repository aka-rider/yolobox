#!/usr/bin/env bash
# Build-time smoke test: resolve every tool from a SYSTEM path and print its
# version. A renamed/broken source therefore fails the build early, and this
# also proves nothing critical was left under $HOME (which the volume would
# mask at runtime). Run as the real user to catch permission traps.
#
# Sources every tools/NN-name.sh module and collects every TOOL_SMOKE entry.
set -u

here="/opt/yolobox/tools"
# shellcheck source=tools/lib.sh
source "${here}/lib.sh"

ALL_SMOKE=()
smoke_collect_cb() {
    if [ -n "${TOOL_SMOKE+x}" ]; then
        ALL_SMOKE+=( "${TOOL_SMOKE[@]}" )
    fi
}
yb_each_module smoke_collect_cb

echo "=== yolobox toolchain smoke ==="
failed=""
for t in "${ALL_SMOKE[@]}"; do
    bin="${t%% *}"
    printf '  %-24s' "${bin}"
    if out="$(eval "${t}" 2>&1)"; then
        echo "OK  $(printf '%s' "${out}" | head -n1)"
    else
        rc=$?
        echo "FAIL (rc=${rc}) path=$(command -v "${bin}" || echo '<not found>')"
        printf '%s\n' "${out}" | head -n 5 | sed 's/^/      /'
        failed="${failed} ${bin}"
    fi
done
if [ -n "${failed}" ]; then
    echo "=== SMOKE FAILED:${failed} ===" >&2
    exit 1
fi
echo "=== all tools resolved from system paths ==="
