#!/usr/bin/env bash
# libvirt qemu hook: keep yolobox-vm's ACL on the passt socket directory.
# linux/host-setup.sh installs it as /etc/libvirt/hooks/qemu.d/50-yolobox-passt.
#
# libvirt runs passt under the domain's DAC label, yolobox-vm, in a directory
# it owns as qemu. host-setup's ACL entry lets yolobox-vm bind there, but
# libvirt recomputes the directory's ACL mask whenever it starts another passt
# domain, and the entry is only as wide as the mask. So the fixed setfacl below
# runs before a yolobox domain starts, and after any domain starts or is
# released.
#
# libvirt calls this as `HOOK <domain> <operation> <sub-operation> <extra>`,
# with the domain XML on stdin. The XML is only searched for the yolobox
# namespace; no value from it reaches a command. Nothing is printed on stdout:
# for some operations libvirt reads stdout as a replacement XML. The hook never
# calls virsh, because libvirt holds the domain while a hook runs.
set -Eeuo pipefail

# Test seams: the tests put a fake setfacl and stat first and point at a
# directory of their own. libvirt passes no PATH and sets neither variable.
PATH="${YOLOBOX_HOOK_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
readonly PASST_DIR="${YOLOBOX_HOOK_PASST_DIR:-/run/libvirt/qemu/passt}"
readonly VM_USER="yolobox-vm"
readonly DOMAIN_NS="https://github.com/aka-rider/yolobox"
# libvirt's own account, which creates the directory (linux/host-setup.sh's
# tmpfiles rule spells the same owner).
readonly DIR_OWNER="qemu"

# The fixed setfacl, run as root on a directory that qemu owns. It is applied
# only to the directory itself (-P: never through a symlink) and only when
# that directory is a real one owned by qemu: a symlink or another owner there
# means something other than libvirt made it, and granting yolobox-vm rwx on
# whatever it points at would hand that account a host path. On failure it says
# so on stderr, which libvirt logs, and returns non-zero: the caller decides
# what that failure stops.
fix_acl() {
    local owner
    # Before the first passt domain the directory may not exist yet; there is
    # nothing to fix then.
    [ -e "${PASST_DIR}" ] || [ -L "${PASST_DIR}" ] || return 0
    if [ -L "${PASST_DIR}" ] || ! [ -d "${PASST_DIR}" ]; then
        printf 'yolobox passt hook: %s is not a directory (a symlink?), not touching its ACL during %s %s of %s\n' \
            "${PASST_DIR}" "$2" "$3" "$1" >&2
        return 1
    fi
    owner=$(stat -c %U "${PASST_DIR}")
    if [ "${owner}" != "${DIR_OWNER}" ]; then
        printf 'yolobox passt hook: %s is owned by %s, not %s, not touching its ACL during %s %s of %s\n' \
            "${PASST_DIR}" "${owner}" "${DIR_OWNER}" "$2" "$3" "$1" >&2
        return 1
    fi
    if ! setfacl -P -m "u:${VM_USER}:rwx,m::rwx" "${PASST_DIR}"; then
        printf 'yolobox passt hook: setfacl on %s failed during %s %s of %s\n' \
            "${PASST_DIR}" "$2" "$3" "$1" >&2
        return 1
    fi
}

main() {
    local domain=${1:-} operation=${2:-} sub=${3:-} xml
    # Read the whole XML, so libvirt never writes into a closed pipe.
    xml=$(cat)
    case "${operation} ${sub}" in
    "prepare begin")
        # A yolobox domain whose passt could not bind should not start: loud.
        if [[ ${xml} == *"${DOMAIN_NS}"* ]]; then
            fix_acl "${domain}" "${operation}" "${sub}" || exit 1
        fi
        ;;
    "started begin" | "release end")
        # Any domain, often not ours: a failure is logged, never turned into
        # a failed start, or a hook left behind with yolobox-vm deleted would
        # stop every VM on the host from starting.
        fix_acl "${domain}" "${operation}" "${sub}" || true
        ;;
    esac
    exit 0
}

main "$@"
