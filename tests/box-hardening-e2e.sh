#!/usr/bin/env bash
# End-to-end acceptance test for the box-hardening change (nix/hardening.nix).
# Runs on the HOST against a throwaway, already bootstrapped lima instance,
# never against a box you use: step 5 reboots it.
#
#   tests/box-hardening-e2e.sh <instance> [EXTRA_WRAPPERS...]
#
# <instance>        lima instance name, as for `yo -m <instance>`. The default
#                   machine (`yolobox`) is the one a person works in, so it is
#                   refused unless FORCE=1 is set.
# EXTRA_WRAPPERS    names (or name=caps) of the setuid wrappers the instance's own
#                   /etc/yolobox/local.nix adds via
#                   yolobox.hardening.extraWrappers (e.g. a VPN client's split-tunnel helper).
# ONLY              passed through to the guest script (hardening or compat);
#                   unset runs both halves.
# YO                the yo to drive the box with; the default is this
#                   checkout's, so the test never runs another install's.
#
# Steps, in order; the first failure stops the run and names its step:
#   1. yo up
#   2. tests/box-hardening-check.sh inside the guest
#   3. yo status: exit 0 (it exits 1 on any warning), egress table in place, network ok
#   4. yo cp round trip of a temp file
#   5. yo down, yo up: the LUKS unlock and a boot with the new kernel
#      parameters. 1Password may ask for Touch ID. The kernel's boot_id must
#      change, or the box never rebooted.
#   6. the guest check again, now on the rebooted kernel
#
# bash 3.2 compatible: macOS /bin/bash is 3.2, so no associative arrays, no
# mapfile, no ${var,,}, and no array expansions that can be empty.
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <instance> [EXTRA_WRAPPERS...]" >&2
    exit 64
fi
inst=$1
shift
extra_wrappers="$*"
only=${ONLY:-}

here=$(cd "$(dirname "$0")" && pwd)
check_script="${here}/box-hardening-check.sh"
yo=${YO:-"${here}/../yo"}
[ -f "${check_script}" ] || { echo "FAIL: ${check_script} is missing" >&2; exit 1; }
[ -x "${yo}" ] || { echo "FAIL: ${yo} is not an executable yo" >&2; exit 1; }

# The default machine is never a throwaway (yolobox_cli/config.py DEFAULT_INSTANCE).
if [ "${inst}" = yolobox ] && [ "${FORCE:-}" != 1 ]; then
    echo "FAIL: '${inst}' is the default machine, which this test reboots; use a throwaway instance, or FORCE=1" >&2
    exit 64
fi

# The names and ONLY travel inside single-quoted words of the remote command
# line, so they must not be able to close the quote. `=` and `,` are in because
# the documented form is name=caps (dumpcap=cap_net_raw,cap_setpcap).
for name in "$@"; do
    case ${name} in
    *[!A-Za-z0-9._,=-]* | '')
        echo "FAIL: '${name}' is not a plausible wrapper name" >&2
        exit 64
        ;;
    esac
done
case ${only} in
'' | hardening | compat) ;;
*)
    echo "FAIL: ONLY must be hardening, compat or unset, got '${only}'" >&2
    exit 64
    ;;
esac

step=""
tmp=$(mktemp -d "${TMPDIR:-/tmp}/box-hardening-e2e.XXXXXX")
guest_dir="/tmp/hk-e2e-$$"
guest_dir_used=""

# The agent owns the guest directory and /tmp is sticky, so the operator
# removes it. A failed removal is reported, not hidden.
cleanup() {
    local status=$?
    if [ -n "${guest_dir_used}" ]; then
        "${yo}" -m "${inst}" ssh "sudo rm -rf ${guest_dir}" </dev/null ||
            echo "WARN: could not remove ${guest_dir} on ${inst}" >&2
    fi
    rm -rf "${tmp}"
    if [ "${status}" -ne 0 ]; then
        echo "FAIL: step '${step}' (instance ${inst})" >&2
    fi
    exit "${status}"
}
trap cleanup EXIT

begin() {
    step=$1
    echo
    echo "=== ${step}"
}

# The check script reads from stdin via `bash -s`, so it needs no copy in the
# guest. yo passes the one word to ssh, which hands it to the operator's shell.
guest_check() {
    "${yo}" -m "${inst}" ssh "EXTRA_WRAPPERS='${extra_wrappers}' ONLY='${only}' bash -s" <"${check_script}"
}

# The kernel's boot id: a new one per boot, so a `yo down; yo up` that left
# the box running shows as the same id.
guest_boot_id() {
    "${yo}" -m "${inst}" ssh cat /proc/sys/kernel/random/boot_id </dev/null
}

# Not `yo ... status | grep`: a failing grep must not hide yo's own exit code.
check_status() {
    local out
    out=$("${yo}" -m "${inst}" status) || {
        printf '%s\n' "${out}"
        echo "yo status exited non-zero: one of its lines above is a warning" >&2
        return 1
    }
    printf '%s\n' "${out}"
    grep -qx 'egress: host and LAN blocked' <<<"${out}" ||
        { echo "status has no 'egress: host and LAN blocked' line" >&2; return 1; }
    grep -q '^network: ok' <<<"${out}" ||
        { echo "status has no 'network: ok' line" >&2; return 1; }
}

# `yo cp` copies as the agent over scp (sftp mode) and accepts an absolute
# guest path under /tmp, so the round trip needs no linked repo and no cwd
# mirror: upload a file to a fresh /tmp directory, download it into an
# existing host directory, compare. It exercises sftp-server, which the
# hardening must not break. The exit trap removes the guest directory.
cp_round_trip() {
    local payload="${tmp}/payload" back="${tmp}/back"
    printf 'box-hardening %s %s\n' "$$" "$(date +%s)" >"${payload}"
    mkdir "${back}"
    guest_dir_used=1
    "${yo}" -m "${inst}" cp "${payload}" ":${guest_dir}/payload"
    "${yo}" -m "${inst}" cp ":${guest_dir}/payload" "${back}"
    cmp "${payload}" "${back}/payload"
}

begin "1. yo up"
"${yo}" -m "${inst}" up
boot_before=$(guest_boot_id)
[ -n "${boot_before}" ] || { echo "the guest printed no boot_id" >&2; exit 1; }

begin "2. guest hardening and compatibility checks"
guest_check

begin "3. yo status"
check_status

begin "4. yo cp round trip"
cp_round_trip

begin "5. yo down, yo up (reboot; 1Password may prompt for the disk key)"
"${yo}" -m "${inst}" down
"${yo}" -m "${inst}" up
boot_after=$(guest_boot_id)
[ -n "${boot_after}" ] || { echo "the guest printed no boot_id after the restart" >&2; exit 1; }
[ "${boot_after}" != "${boot_before}" ] ||
    { echo "the boot_id is still ${boot_before}: the box did not reboot" >&2; exit 1; }
echo "boot_id ${boot_before} -> ${boot_after}"

begin "6. guest checks again after the reboot"
guest_check

echo
echo "box-hardening-e2e: all steps passed on ${inst}"
