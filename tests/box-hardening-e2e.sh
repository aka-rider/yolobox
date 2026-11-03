#!/usr/bin/env bash
# End-to-end acceptance test for the box-hardening change (nix/hardening.nix).
# Runs on the HOST against a throwaway, already bootstrapped lima instance,
# never against a box you use: step 5 reboots it.
#
#   tests/box-hardening-e2e.sh <instance> [EXTRA_WRAPPERS...]
#
# <instance>        lima instance name, as for `yo -m <instance>`.
# EXTRA_WRAPPERS    names of the setuid wrappers the instance's own
#                   /etc/yolobox/local.nix adds via
#                   yolobox.hardening.extraWrappers (e.g. a VPN client's split-tunnel helper).
#
# Steps, in order; the first failure stops the run and names its step:
#   1. yo up
#   2. tests/box-hardening-check.sh inside the guest
#   3. yo status: exit 0 (it exits 1 on any warning), egress table in place, network ok
#   4. yo cp round trip of a temp file
#   5. yo down, yo up: the LUKS unlock and a boot with the new kernel
#      parameters. 1Password may ask for Touch ID.
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

here=$(cd "$(dirname "$0")" && pwd)
check_script="${here}/box-hardening-check.sh"
[ -f "${check_script}" ] || { echo "FAIL: ${check_script} is missing" >&2; exit 1; }
command -v yo >/dev/null || { echo "FAIL: yo is not on PATH" >&2; exit 1; }

# The names travel inside one single-quoted word of the remote command line,
# so they must not be able to close the quote.
for name in "$@"; do
    case ${name} in
    *[!A-Za-z0-9._-]* | '')
        echo "FAIL: '${name}' is not a plausible wrapper name" >&2
        exit 64
        ;;
    esac
done

step=""
tmp=$(mktemp -d "${TMPDIR:-/tmp}/box-hardening-e2e.XXXXXX")
guest_dir="/tmp/hk-e2e-$$"
guest_dir_used=""

# The agent owns the guest directory and /tmp is sticky, so the operator
# removes it. A failed removal is reported, not hidden.
cleanup() {
    local status=$?
    if [ -n "${guest_dir_used}" ]; then
        yo -m "${inst}" ssh "sudo rm -rf ${guest_dir}" </dev/null ||
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
    yo -m "${inst}" ssh "EXTRA_WRAPPERS='${extra_wrappers}' bash -s" <"${check_script}"
}

# Not `yo ... status | grep`: a failing grep must not hide yo's own exit code.
check_status() {
    local out
    out=$(yo -m "${inst}" status) || {
        printf '%s\n' "${out}"
        echo "yo status exited non-zero: one of its lines above is a warning" >&2
        return 1
    }
    printf '%s\n' "${out}"
    printf '%s\n' "${out}" | grep -qx 'egress: host and LAN blocked' ||
        { echo "status has no 'egress: host and LAN blocked' line" >&2; return 1; }
    printf '%s\n' "${out}" | grep -q '^network: ok' ||
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
    yo -m "${inst}" cp "${payload}" ":${guest_dir}/payload"
    yo -m "${inst}" cp ":${guest_dir}/payload" "${back}"
    cmp "${payload}" "${back}/payload"
}

begin "1. yo up"
yo -m "${inst}" up

begin "2. guest hardening and compatibility checks"
guest_check

begin "3. yo status"
check_status

begin "4. yo cp round trip"
cp_round_trip

begin "5. yo down, yo up (reboot; 1Password may prompt for the disk key)"
yo -m "${inst}" down
yo -m "${inst}" up

begin "6. guest checks again after the reboot"
guest_check

echo
echo "box-hardening-e2e: all steps passed on ${inst}"
