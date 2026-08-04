#!/usr/bin/env bash
# The ONE way to build yolobox:local, and the ONE way to regenerate the
# Dockerfile's generated ENV region + tools/runtime-env.generated from every
# tools/*.sh module's TOOL_ENV / TOOL_PATH / TOOL_RUNTIME_ENV.
#
# `--regen-only` regenerates and exits WITHOUT building -- the idempotence
# gate ("run --regen-only twice, diff both generated artifacts") must not
# cost two full image builds. Default (no flag): regenerate, then build.
#
# Must work on macOS bash 3.2 with BSD sed/awk -- this runs on the host, not
# in the image.
#
# The username sanitisation MUST match the launcher's derivation exactly,
# including the leading-digit fallback -- otherwise the image's user disagrees
# with the launcher and the differing build args thrash the whole
# toolchain layer.
set -euo pipefail

# Resolve the repo root without `readlink -f`: on BSD/macOS it returns rc=1 and
# EMPTY stdout for a missing leaf, and `dirname ""` is `.`, so a renamed script
# would silently build the CURRENT directory as the Docker context.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd "${here}/.." && pwd -P)"

# shellcheck source=tools/lib.sh
source "${here}/lib.sh"

regen_only=0
for arg in "$@"; do
    case "${arg}" in
        --regen-only) regen_only=1 ;;
        *) echo "build.sh: unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Collect TOOL_ENV / TOOL_PATH / TOOL_RUNTIME_ENV from every module, in
# yb_each_module's LC_ALL=C filename order -- the SAME order
# install-all.sh's drift guard iterates in, which is what makes the two
# comparable.
# ---------------------------------------------------------------------------
# TOOL_ENV entries must be KEY=VALUE with a shell-safe KEY and a
# whitespace-free VALUE -- Docker's `ENV NAME VALUE` form splits on the first
# space, so a value containing a space (e.g. `FOO=bar baz`) would silently
# become a SECOND ENV var (`baz`) instead of part of FOO's value.
tool_env_key_re='^[A-Za-z_][A-Za-z0-9_]*='
# Parallel arrays, not an associative array: this script must run on macOS's
# stock bash 3.2, which has no `declare -A`.
SEEN_ENV_KEYS=()
SEEN_ENV_MODULES=()

ALL_ENV=()
ALL_PATH=()
ALL_RUNTIME_ENV=()
collect_cb() {
    local m="$1" entry key i seen_mod
    if [ -n "${TOOL_ENV+x}" ]; then
        for entry in "${TOOL_ENV[@]}"; do
            if [[ ! "${entry}" =~ ${tool_env_key_re} ]] || [[ "${entry}" == *[[:space:]]* ]]; then
                echo "build.sh: ${m} declares a malformed TOOL_ENV entry '${entry}' (expected KEY=VALUE, KEY matching ^[A-Za-z_][A-Za-z0-9_]*=, VALUE with no whitespace)" >&2
                exit 1
            fi
            key="${entry%%=*}"
            seen_mod=""
            for i in "${!SEEN_ENV_KEYS[@]}"; do
                if [ "${SEEN_ENV_KEYS[$i]}" = "${key}" ]; then
                    seen_mod="${SEEN_ENV_MODULES[$i]}"
                    break
                fi
            done
            if [ -n "${seen_mod}" ]; then
                echo "build.sh: TOOL_ENV key '${key}' is declared by both ${seen_mod} and ${m}" >&2
                exit 1
            fi
            SEEN_ENV_KEYS+=( "${key}" )
            SEEN_ENV_MODULES+=( "${m}" )
            ALL_ENV+=( "${entry}" )
        done
    fi
    if [ -n "${TOOL_PATH+x}" ]; then ALL_PATH+=( "${TOOL_PATH[@]}" ); fi
    if [ -n "${TOOL_RUNTIME_ENV+x}" ]; then ALL_RUNTIME_ENV+=( "${TOOL_RUNTIME_ENV[@]}" ); fi
}
yb_each_module collect_cb

# @HOME@ -> the LITERAL string "/home/${USERNAME}", never shell-expanded here
# -- Docker expands the ARG at build time, once, in the image. Modules never
# write $HOME/${HOME} themselves, which is what makes this blind
# substitution on the macOS host safe: nothing here can ever be a per-
# developer host path.
home_literal='/home/${USERNAME}'

dockerfile="${root}/Dockerfile"
runtime_file="${root}/tools/runtime-env.generated"

start_marker="# >>> generated from tools/*.sh by tools/build.sh — DO NOT EDIT BY HAND <<<"
end_marker="# >>> end generated <<<"

grep -qF "${start_marker}" "${dockerfile}" || { echo "build.sh: start marker not found in Dockerfile" >&2; exit 1; }
grep -qF "${end_marker}" "${dockerfile}" || { echo "build.sh: end marker not found in Dockerfile" >&2; exit 1; }

gen_body="$(mktemp)"
before_file="$(mktemp)"
after_file="$(mktemp)"
new_dockerfile="$(mktemp)"
cleanup() { rm -f "${gen_body}" "${before_file}" "${after_file}" "${new_dockerfile}"; }
trap cleanup EXIT

# Everything up to and INCLUDING the start marker line, byte-for-byte.
awk -v m="${start_marker}" '{print} index($0,m){exit}' "${dockerfile}" > "${before_file}"
# Everything from the end marker line (inclusive) to EOF, byte-for-byte.
awk -v m="${end_marker}" 'index($0,m){f=1} f{print}' "${dockerfile}" > "${after_file}"

{
    for entry in "${ALL_ENV[@]+"${ALL_ENV[@]}"}"; do
        printf 'ENV %s\n' "${entry//@HOME@/${home_literal}}"
    done
    path_joined=""
    for p in "${ALL_PATH[@]+"${ALL_PATH[@]}"}"; do
        path_joined="${path_joined}${p}:"
    done
    printf 'ENV PATH=%s%s\n' "${path_joined}" "${YB_FIXED_PATH_TAIL}"
} > "${gen_body}"

cat "${before_file}" "${gen_body}" "${after_file}" > "${new_dockerfile}"
cp "${new_dockerfile}" "${dockerfile}"

# tools/runtime-env.generated: applied at `docker exec` only, by the launcher
# -- @HOME@ is left UNSUBSTITUTED here (the launcher substitutes ${CTR_HOME}
# at exec time, since only it knows the runtime home path).
{
    printf '# generated by tools/build.sh from tools/*.sh TOOL_RUNTIME_ENV -- DO NOT EDIT BY HAND\n'
    printf '# @HOME@ is substituted by the launcher (yolobox) at exec time, not here.\n'
    for entry in "${ALL_RUNTIME_ENV[@]+"${ALL_RUNTIME_ENV[@]}"}"; do
        printf '%s\n' "${entry}"
    done
} > "${runtime_file}"

if [ "${regen_only}" -eq 1 ]; then
    echo "build.sh: regenerated Dockerfile ENV region and tools/runtime-env.generated"
    exit 0
fi

# `exec` replaces this shell, so the EXIT trap below would never fire --
# clean up the mktemp files explicitly first, then propagate docker build's
# exit code by exec-ing into it.
cleanup
trap - EXIT
exec docker build -t yolobox:local \
    --build-arg "USERNAME=$("${here}/box-user.sh")" \
    --build-arg "UID=$(id -u)" --build-arg "GID=$(id -g)" \
    "${root}"
