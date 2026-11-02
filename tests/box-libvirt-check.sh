#!/usr/bin/env bash
# Acceptance check for a yolobox box on a Linux host (libvirt, passt, sVirt).
# Runs on the HOST, against a box that is up and bootstrapped.
#
#   bash tests/box-libvirt-check.sh NAME
#
# NAME is the instance name, as for `yo -m NAME`. It reaches the box over the
# instance's own ssh.config, so it works before the agent account exists.
#
# What it checks, in order, and stops at the first broken property:
#   (a) libvirt's SELinux label of the domain: svirt_t, a category pair, enforcing
#   (b) QEMU and passt run as yolobox-vm, in svirt_t and passt_t, with that pair
#   (c) the live domain XML: no graphics, vsock, filesystem, channel, hostdev
#       or tpm, the ssh forward bound to 127.0.0.1, the yolobox metadata
#   (d) with the guest's own egress table deleted, so that only the host layer
#       (linux/yolobox-host.nft) is left, the guest cannot reach the host, the
#       LAN or a peer box, and still reaches the internet and DNS
#   (e) the ssh forward is bound on 127.0.0.1 only
#
# Step (d) CHANGES THE GUEST: it deletes the table inet yolobox-egress and
# restores it afterwards by restarting yolobox-egress.service, which an EXIT
# trap does on every exit path a shell can handle (failure, Ctrl-C, kill).
# While the table is gone the agent's traffic is judged by the host table
# alone. Never run this against a box that is mid-work. After a kill -9 of
# this script, or a lost ssh connection, restore by hand:
#   yo -m NAME ssh sudo systemctl restart yolobox-egress
#
# Lines it prints besides "ok:":
#   SKIP peer       no other yolobox domain is running, so the box-to-box
#                   probe did not run (start a second instance to run it)
#   SKIP tailnet    the host or the box is not on a tailnet
#   RESIDUAL tailnet-host-ssh
#                   the box reached the host's sshd over the tailnet. This is
#                   the known residual in docs/linux-host.md, so it does not
#                   change the exit status.
set -Eeuo pipefail

readonly URI=qemu:///system
# dumpxml prints the metadata element with double quotes and the rest of the
# XML with single ones, so the greps for the metadata accept either.
readonly YOLOBOX_NS=https://github.com/aka-rider/yolobox
readonly VM_ACCOUNT=yolobox-vm
readonly GATEWAY_ADDR=192.168.5.2
# nix/egress.nix: the unit that loads the guest table, and the table itself.
readonly EGRESS_UNIT=yolobox-egress
readonly EGRESS_TABLE="inet yolobox-egress"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

ok() {
    echo "ok: $*"
}

if [ "$#" -ne 1 ]; then
    echo "usage: $0 NAME" >&2
    exit 64
fi
NAME=$1
case ${NAME} in
'' | *[!a-z0-9-]* | -*) fail "'${NAME}' is not an instance name (lowercase letters, digits, dashes)" ;;
esac
DOMAIN="yolobox-${NAME}"
SSH_CONFIG="${HOME}/.local/state/yolobox/instances/${NAME}/ssh.config"
SSH_HOST="lima-${NAME}"
[ -f "${SSH_CONFIG}" ] || fail "${SSH_CONFIG} is missing: run yo -m ${NAME} up first"

TABLE_DELETED=""
CANARY_PID=""
CANARY_PORT=""
CATEGORIES=""
SSH_PORT=""

trap 'echo "FAIL: unexpected error at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

virsh_system() {
    virsh -c "${URI}" "$@"
}

# Runs one command line in the guest as the operator. The remote shell parses
# it, so callers quote for it. Exit 255 is ssh's own failure.
guest() {
    ssh -F "${SSH_CONFIG}" "${SSH_HOST}" "$@" </dev/null
}

# Restarting the unit reloads recovery and then the full rules, so the table
# is the real one again, whatever state it was left in.
restore_egress_table() {
    guest "sudo systemctl restart ${EGRESS_UNIT}" || return 1
    guest "sudo nft list table ${EGRESS_TABLE} >/dev/null" || return 1
    TABLE_DELETED=""
}

cleanup() {
    local status=$?
    trap - ERR
    if [ -n "${CANARY_PID}" ]; then
        kill "${CANARY_PID}" || echo "WARN: could not stop the canary listener ${CANARY_PID}" >&2
    fi
    if [ -n "${TABLE_DELETED}" ] && ! restore_egress_table; then
        echo "FAIL: the guest egress table is NOT restored. Run: yo -m ${NAME} ssh sudo systemctl restart ${EGRESS_UNIT}" >&2
        status=1
    fi
    exit "${status}"
}
trap cleanup EXIT
# A signal ends the script through exit, so the EXIT trap runs for it too.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# True when the guest reaches HOST:PORT within 5 s. Fails the run when ssh to
# the guest breaks, which would otherwise read as a blocked connection.
guest_connects() {
    local host=$1 port=$2 status=0
    guest "timeout 5 bash -c '</dev/tcp/${host}/${port}'" >/dev/null 2>&1 || status=$?
    case ${status} in
    0) return 0 ;;
    255) fail "ssh to ${SSH_HOST} failed while probing ${host}:${port}" ;;
    *) return 1 ;;
    esac
}

expect_blocked() {
    local label=$1 host=$2 port=$3
    if guest_connects "${host}" "${port}"; then
        fail "the guest connected to ${label} (${host}:${port})"
    fi
    ok "the guest cannot reach ${label} (${host}:${port})"
}

# A listener the host owns, on every address, so a probe that fails to reach it
# was refused by the layers and not by a closed port. Port 22 on the host may
# well have no sshd.
start_canary() {
    # A simple command, not a brace group: COPROC_PID is then python's own pid.
    coproc python3 -c 'import socket
s = socket.socket()
s.bind(("0.0.0.0", 0))
s.listen(8)
print(s.getsockname()[1], flush=True)
while True:
    s.accept()[0].close()'
    CANARY_PID=${COPROC_PID}
    read -r -u "${COPROC[0]}" CANARY_PORT ||
        fail "the canary listener printed no port"
}

# ---------------------------------------------------------------- (a), (b)

check_domain_label() {
    local info line
    info=$(virsh_system dominfo "${DOMAIN}") || fail "virsh dominfo ${DOMAIN} failed"
    case ${info} in
    *"Security model:"*selinux*) ;;
    *) fail "${DOMAIN} has no selinux security model: ${info}" ;;
    esac
    line=$(sed -n 's/^Security label:[[:space:]]*//p' <<<"${info}")
    [[ ${line} =~ :svirt_t:s0:(c[0-9]+,c[0-9]+)[[:space:]]+\(enforcing\)$ ]] ||
        fail "the label of ${DOMAIN} is '${line}', want svirt_t:s0:cN,cM (enforcing)"
    CATEGORIES=${BASH_REMATCH[1]}
    ok "${DOMAIN} is labelled svirt_t:s0:${CATEGORIES} (enforcing)"
}

# Counts the processes of one program that run as the VM account in one
# SELinux type with the domain's category pair. $1 is a comm prefix, $2 the type.
count_confined() {
    local comm=$1 type=$2 uid=$3 procs=$4
    awk -v comm="${comm}" -v want=":${type}:s0:${CATEGORIES}" -v uid="${uid}" '
        $1 == uid && $3 ~ ("^" comm) && substr($2, length($2) - length(want) + 1) == want { n++ }
        END { print n + 0 }' <<<"${procs}"
}

check_process_confinement() {
    local uid procs qemu passt
    uid=$(id -u "${VM_ACCOUNT}") || fail "the account ${VM_ACCOUNT} does not exist: run linux/host-setup.sh"
    procs=$(ps -eo euid=,label=,comm=) || fail "ps could not list processes with their labels"
    qemu=$(count_confined qemu-system svirt_t "${uid}" "${procs}")
    passt=$(count_confined passt passt_t "${uid}" "${procs}")
    [ "${qemu}" -eq 1 ] ||
        fail "want exactly one qemu process as ${VM_ACCOUNT} in svirt_t:s0:${CATEGORIES}, found ${qemu}"
    [ "${passt}" -ge 1 ] ||
        fail "no passt process as ${VM_ACCOUNT} in passt_t:s0:${CATEGORIES}"
    ok "qemu runs as ${VM_ACCOUNT} in svirt_t and ${passt} passt in passt_t, all in s0:${CATEGORIES}"
}

# ------------------------------------------------------------------- (c)

check_domain_xml() {
    local xml device
    xml=$(virsh_system dumpxml "${DOMAIN}") || fail "virsh dumpxml ${DOMAIN} failed"
    for device in graphics vsock filesystem channel hostdev tpm; do
        if grep -q "<${device}[ />]" <<<"${xml}"; then
            fail "${DOMAIN} has a ${device} device"
        fi
    done
    grep -Eq "<portForward[^>]*address='127\.0\.0\.1'" <<<"${xml}" ||
        fail "${DOMAIN} has no portForward bound to 127.0.0.1"
    grep -Eq "<yolobox:instance xmlns:yolobox=['\"]${YOLOBOX_NS}['\"]>${NAME}</yolobox:instance>" <<<"${xml}" ||
        fail "${DOMAIN} lacks the yolobox:instance metadata for ${NAME}"
    SSH_PORT=$(ssh_port_of "${xml}")
    ok "${DOMAIN} has no graphics, vsock, filesystem, channel, hostdev or tpm, forwards 127.0.0.1:${SSH_PORT} and carries the metadata"
}

# The host port that forwards to the guest's port 22, from a domain's XML.
ssh_port_of() {
    local port
    port=$(sed -n "s/.*<range start='\([0-9][0-9]*\)' to='22'.*/\1/p" <<<"$1")
    [[ ${port} =~ ^[0-9]+$ ]] || fail "no forward to guest port 22 in the domain XML"
    echo "${port}"
}

# ------------------------------------------------------------------- (d)

# Prints "NAME PORT" for every other running domain tagged as a yolobox one.
running_peers() {
    local other xml names port
    names=$(virsh_system list --name) || fail "virsh list failed"
    while IFS= read -r other; do
        if [ -z "${other}" ] || [ "${other}" = "${DOMAIN}" ]; then
            continue
        fi
        xml=$(virsh_system dumpxml "${other}") || fail "virsh dumpxml ${other} failed"
        if grep -Eq "xmlns:yolobox=['\"]${YOLOBOX_NS}['\"]" <<<"${xml}"; then
            port=$(ssh_port_of "${xml}")
            echo "${other} ${port}"
        fi
    done <<<"${names}"
}

check_peers() {
    local peers peer_name peer_port seen=""
    peers=$(running_peers)
    while read -r peer_name peer_port; do
        if [ -z "${peer_name}" ]; then
            continue
        fi
        seen=1
        expect_blocked "the ssh port of the running box ${peer_name}" "${GATEWAY_ADDR}" "${peer_port}"
    done <<<"${peers}"
    if [ -z "${seen}" ]; then
        echo "SKIP peer"
    fi
}

check_tailnet() {
    local addrs tail_ip status=0
    if [ ! -d /sys/class/net/tailscale0 ]; then
        echo "SKIP tailnet"
        return
    fi
    addrs=$(ip -4 -o addr show dev tailscale0) || fail "ip addr show dev tailscale0 failed"
    tail_ip=$(awk '{ sub("/.*", "", $4); print $4; exit }' <<<"${addrs}")
    if [ -z "${tail_ip}" ]; then
        echo "SKIP tailnet"
        return
    fi
    guest "tailscale status >/dev/null 2>&1" || status=$?
    case ${status} in
    0) ;;
    255) fail "ssh to ${SSH_HOST} failed while asking for tailscale status" ;;
    *)
        echo "SKIP tailnet"
        return
        ;;
    esac
    if guest_connects "${tail_ip}" 22; then
        echo "RESIDUAL tailnet-host-ssh"
    else
        ok "the guest cannot reach the host's tailnet address on port 22"
    fi
}

check_host_layer_alone() {
    local route host_ip default_gw answer
    route=$(ip -4 route get 1.1.1.1) || fail "the host has no route to 1.1.1.1"
    host_ip=$(awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' <<<"${route}")
    default_gw=$(awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }' <<<"${route}")
    [ -n "${host_ip}" ] || fail "no source address in the host's route to 1.1.1.1: ${route}"

    TABLE_DELETED=1
    guest "sudo nft delete table ${EGRESS_TABLE}" || fail "could not delete the guest egress table"
    ok "the guest egress table is deleted; the host layer is all that is left"

    start_canary
    expect_blocked "the gateway, the host's loopback, ssh" "${GATEWAY_ADDR}" 22
    expect_blocked "the gateway, a listener of the host" "${GATEWAY_ADDR}" "${CANARY_PORT}"
    expect_blocked "the host's primary address, ssh" "${host_ip}" 22
    expect_blocked "the host's primary address, a listener of the host" "${host_ip}" "${CANARY_PORT}"
    if [ -n "${default_gw}" ]; then
        expect_blocked "the LAN's default gateway" "${default_gw}" 80
    else
        echo "SKIP lan-gateway"
    fi
    check_peers
    check_tailnet

    guest "curl -sSI --max-time 20 https://github.com >/dev/null" ||
        fail "the guest cannot fetch https://github.com with the host layer alone"
    ok "the guest reaches https://github.com"
    answer=$(guest "dig +short +time=5 +tries=1 @${GATEWAY_ADDR} github.com") ||
        fail "dig @${GATEWAY_ADDR} github.com failed in the guest"
    [ -n "${answer}" ] || fail "dig @${GATEWAY_ADDR} github.com answered nothing"
    ok "the gateway answers DNS"

    restore_egress_table || fail "could not restore the guest egress table"
    ok "the guest egress table is back"
}

# ------------------------------------------------------------------- (e)

check_forward_bound_to_loopback() {
    local listeners locals
    listeners=$(ss -ltnH "sport = :${SSH_PORT}") || fail "ss failed"
    locals=$(awk '{ print $4 }' <<<"${listeners}")
    [ -n "${locals}" ] || fail "nothing listens on port ${SSH_PORT}"
    [ "$(sort -u <<<"${locals}")" = "127.0.0.1:${SSH_PORT}" ] ||
        fail "port ${SSH_PORT} is bound beyond 127.0.0.1: ${locals}"
    ok "ssh port ${SSH_PORT} is bound on 127.0.0.1 only"
}

main() {
    local state
    state=$(virsh_system domstate "${DOMAIN}") || fail "no domain ${DOMAIN}: is the box created?"
    [ "${state}" = running ] || fail "${DOMAIN} is ${state}, not running"
    guest true || fail "cannot ssh to ${SSH_HOST} with ${SSH_CONFIG}"
    check_domain_label
    check_process_confinement
    check_domain_xml
    check_host_layer_alone
    check_forward_bound_to_loopback
    echo "box-libvirt-check: all checks passed"
}

main
