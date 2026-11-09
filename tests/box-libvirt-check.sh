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
#   (c) the live domain XML is the box's own (the yolobox metadata), holds no
#       device that reaches the host (graphics, vsock, filesystem, channel,
#       hostdev, tpm), one passt interface, both seclabels relabelling, and
#       every portForward bound to 127.0.0.1
#   (d) with the guest's own egress table deleted, so that only the host layer
#       (linux/yolobox-host.nft) is left, the guest cannot reach the host, the
#       LAN or a peer box, over TCP and UDP, on IPv4 and on IPv6 (the gateway
#       and the host's global address), and still reaches the internet and DNS
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
# A probe counts as blocked only when it timed out or was refused; a probe
# that fails any other way (no route, a missing tool) fails the run. A probe
# is run only against a listener the host itself can reach, so a "blocked"
# never means "nothing listens": the host's own canary for its addresses, and
# a host-side connect first for the LAN gateway and the tailnet address.
#
# Lines it prints besides "ok:":
#   SKIP peer       no other yolobox domain is running, so the box-to-box
#                   probe did not run (start a second instance to run it)
#   SKIP lan-gateway
#                   the host has no LAN default gateway, or that gateway's
#                   port 80 does not answer the host, so the LAN probe did
#                   not run
#   SKIP tailnet    the host or the box is not on a tailnet, or the host's
#                   tailnet address does not answer the host on port 22
#   SKIP v6-gateway the guest has no IPv6 default route, or the host cannot
#                   bind an IPv6 listener, so the probe through the IPv6
#                   gateway (mapped to the host's ::1) did not run
#   SKIP v6-host    the host has no global IPv6 route, or the guest has no
#                   IPv6 default route, so the probe to the host's global
#                   IPv6 address did not run
#   RESIDUAL tailnet-host-ssh
#                   the box reached the host's sshd over the tailnet. A host
#                   sshd listening on 0.0.0.0 is reachable from a box on the
#                   same tailnet by design, so it does not change the exit status.
set -Eeuo pipefail
# Without this a command substitution runs with errexit off, so `peers=$(f)`
# would carry on past a failing step inside f.
shopt -s inherit_errexit
# The checks read virsh's, ss's and nft's words; a translated tool would say
# something else.
export LC_ALL=C

readonly URI=qemu:///system
# The positive control for the probes: a host on the internet the host layer
# lets the guest reach, and its port.
readonly INTERNET_HOST=github.com
readonly INTERNET_PORT=443
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
DOMAIN="yolobox-${NAME}"
SSH_CONFIG="${HOME}/.local/state/yolobox/instances/${NAME}/ssh.config"
SSH_HOST="lima-${NAME}"
[ -f "${SSH_CONFIG}" ] || fail "${SSH_CONFIG} is missing: run yo -m ${NAME} up first"

YOLOBOX_GATEWAY=""
YOLOBOX_VM_USER=""
YOLOBOX_EGRESS_UNIT=""
EGRESS_TABLE=""
TABLE_DELETED=""
CANARY_PID=""
CANARY_PORT=""
CANARY6_PORT=""
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

# The facts the box renders from facts.json into /etc/yolobox/facts.sh: the
# gateway, the VM account and the egress unit, whose table carries its name.
# The guest sources the file and prints one value per line; the host only
# reads them, it never runs what the guest says.
load_box_facts() {
    local values
    # shellcheck disable=SC2016 # expanded by the guest's shell
    values=$(guest '. /etc/yolobox/facts.sh && printf "%s\n" "$YOLOBOX_GATEWAY" "$YOLOBOX_VM_USER" "$YOLOBOX_EGRESS_UNIT"') ||
        fail "the box has no /etc/yolobox/facts.sh: run yo -m ${NAME} bootstrap first"
    { read -r YOLOBOX_GATEWAY && read -r YOLOBOX_VM_USER && read -r YOLOBOX_EGRESS_UNIT; } <<<"${values}" ||
        fail "the box's /etc/yolobox/facts.sh lacks the gateway, the VM account or the egress unit"
    [ -n "${YOLOBOX_GATEWAY}" ] && [ -n "${YOLOBOX_VM_USER}" ] && [ -n "${YOLOBOX_EGRESS_UNIT}" ] ||
        fail "the box's /etc/yolobox/facts.sh lacks the gateway, the VM account or the egress unit"
    EGRESS_TABLE="inet ${YOLOBOX_EGRESS_UNIT}"
}

# Restarting the unit reloads the rules, so the table is the real one again,
# whatever state it was left in.
restore_egress_table() {
    guest "sudo systemctl restart ${YOLOBOX_EGRESS_UNIT}" || return 1
    guest "sudo nft list table ${EGRESS_TABLE} >/dev/null" || return 1
    TABLE_DELETED=""
}

cleanup() {
    local status=$?
    # A second Ctrl-C must not cut the restore short.
    trap '' INT TERM HUP
    trap - ERR
    if [ -n "${CANARY_PID}" ]; then
        kill "${CANARY_PID}" || echo "WARN: could not stop the canary listener ${CANARY_PID}" >&2
    fi
    if [ -n "${TABLE_DELETED}" ] && ! restore_egress_table; then
        echo "FAIL: the guest egress table is NOT restored. Run: yo -m ${NAME} ssh sudo systemctl restart ${YOLOBOX_EGRESS_UNIT}" >&2
        status=1
    fi
    exit "${status}"
}
trap cleanup EXIT
# A signal ends the script through exit, so the EXIT trap runs for it too.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# How one connect attempt ended, from the exit status and the stderr of a
# `timeout 5 bash -c '</dev/tcp/...'` (or a UDP exchange): "connected",
# "timeout", "refused", or the text of any other failure. The last is never
# taken for a block: a missing tool, a dead ssh or no route proves nothing
# about the layers.
classify_probe() {
    local status=$1 out=$2
    case ${status} in
    0) echo connected ;;
    124) echo timeout ;;
    *)
        case ${out} in
        *"Connection refused"*) echo refused ;;
        *) echo "exit ${status}: ${out}" ;;
        esac
        ;;
    esac
}

# Prints how the guest's TCP connect to HOST:PORT ended (classify_probe).
# Fails the run when ssh to the guest breaks.
guest_tcp() {
    local host=$1 port=$2 status=0 out
    out=$(guest "timeout 5 bash -c '</dev/tcp/${host}/${port}'" 2>&1) || status=$?
    [ "${status}" -ne 255 ] || fail "ssh to ${SSH_HOST} failed while probing ${host}:${port}"
    classify_probe "${status}" "${out}"
}

# Prints how the guest's UDP exchange with HOST:PORT ended: the guest sends
# one datagram and waits for the canary's echo. A reject reaches the socket
# as "Connection refused" on the read, a drop as the timeout.
guest_udp() {
    local host=$1 port=$2 status=0 out
    out=$(guest "timeout 5 bash -c 'exec 3<>/dev/udp/${host}/${port}; echo probe >&3; head -c 5 <&3 >/dev/null'" 2>&1) || status=$?
    [ "${status}" -ne 255 ] || fail "ssh to ${SSH_HOST} failed while probing udp ${host}:${port}"
    classify_probe "${status}" "${out}"
}

# True when the host itself reaches HOST:PORT over TCP: the canary for a
# listener the host does not own.
host_connects() {
    timeout 5 bash -c "</dev/tcp/$1/$2" 2>/dev/null
}

expect_blocked() {
    local label=$1 host=$2 port=$3 outcome
    outcome=$(guest_tcp "${host}" "${port}")
    case ${outcome} in
    connected) fail "the guest connected to ${label} (${host}:${port})" ;;
    timeout | refused) ok "the guest cannot reach ${label} (${host}:${port}, ${outcome})" ;;
    *) fail "the probe of ${label} (${host}:${port}) ended neither connected nor blocked: ${outcome}" ;;
    esac
}

expect_udp_blocked() {
    local label=$1 host=$2 port=$3 outcome
    outcome=$(guest_udp "${host}" "${port}")
    case ${outcome} in
    connected) fail "the guest exchanged UDP with ${label} (${host}:${port})" ;;
    timeout | refused) ok "the guest cannot reach ${label} over UDP (${host}:${port}, ${outcome})" ;;
    *) fail "the UDP probe of ${label} (${host}:${port}) ended neither connected nor blocked: ${outcome}" ;;
    esac
}

# Listeners the host owns, on every address of each family, so a probe that
# fails to reach one was refused by the layers and not by a closed port. Port 22
# on the host may well have no sshd. TCP connections are accepted and closed;
# UDP datagrams are echoed, so a guest that got through sees its probe back.
# It prints the IPv4 port, then the IPv6 port, or 0 for the IPv6 one when the
# host cannot bind it (IPv6 disabled); each port is both TCP and UDP.
start_canary() {
    # A simple command, not a brace group: COPROC_PID is then python's own pid.
    coproc python3 -c 'import select, socket

def listener(family, address, port=0):
    tcp = socket.socket(family, socket.SOCK_STREAM)
    udp = socket.socket(family, socket.SOCK_DGRAM)
    for s in (tcp, udp):
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    tcp.bind((address, port))
    tcp.listen(8)
    try:
        udp.bind((address, tcp.getsockname()[1]))
    except OSError:
        if port:
            raise
        tcp.close()
        udp.close()
        return listener(family, address, port=0)
    print(tcp.getsockname()[1], flush=True)
    return [tcp, udp]

sockets = listener(socket.AF_INET, "0.0.0.0")
try:
    sockets += listener(socket.AF_INET6, "::")
except OSError:
    print(0, flush=True)
while True:
    for ready in select.select(sockets, [], [])[0]:
        if ready.type == socket.SOCK_STREAM:
            ready.accept()[0].close()
        else:
            data, peer = ready.recvfrom(64)
            ready.sendto(data, peer)'
    CANARY_PID=${COPROC_PID}
    read -r -u "${COPROC[0]}" CANARY_PORT ||
        fail "the canary listener printed no port"
    read -r -u "${COPROC[0]}" CANARY6_PORT ||
        fail "the canary listener printed no IPv6 port"
}

# Prints the guest's IPv6 default gateway, scoped when it is link-local (a
# connect to fe80:: needs the interface), or nothing when the guest has no
# IPv6 default route. passt answers for its gateway address of each family and
# maps it to the host's loopback, so this is the IPv6 twin of ${YOLOBOX_GATEWAY}.
guest_v6_gateway() {
    local routes via
    routes=$(guest "ip -6 route show default") || fail "could not list the guest's IPv6 routes"
    via=$(awk '{
        gw = ""; dev = ""
        for (i = 1; i < NF; i++) {
            if ($i == "via") gw = $(i + 1)
            if ($i == "dev") dev = $(i + 1)
        }
        if (gw != "") { print (gw ~ /^fe80:/ ? gw "%" dev : gw); exit }
    }' <<<"${routes}")
    echo "${via}"
}

# The host's default route of one family (-4 or -6) that is not the tailnet's,
# as "GATEWAY DEVICE" (the gateway empty on a point-to-point link), or nothing.
# A host that sends the internet through a tailnet exit node has a default
# route over tailscale0 too, and `ip route get` would answer with the tunnel's
# address; the LAN route is the one the host's own listeners sit on.
lan_default_route() {
    local routes
    routes=$(ip "$1" route show default) || fail "ip $1 route show default failed"
    awk '{
        gw = ""; dev = ""
        for (i = 1; i < NF; i++) {
            if ($i == "via") gw = $(i + 1)
            if ($i == "dev") dev = $(i + 1)
        }
        if (dev != "" && dev != "tailscale0") { print gw, dev; exit }
    }' <<<"${routes}"
}

# The host's global address of one family on a device, or nothing.
host_addr_on() {
    local addrs
    addrs=$(ip "$1" -o addr show dev "$2" scope global) || fail "ip $1 addr show dev $2 failed"
    awk '{ sub("/.*", "", $4); print $4; exit }' <<<"${addrs}"
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
    uid=$(id -u "${YOLOBOX_VM_USER}") || fail "the account ${YOLOBOX_VM_USER} does not exist: run linux/host-setup.sh"
    procs=$(ps -eo euid=,label=,comm=) || fail "ps could not list processes with their labels"
    qemu=$(count_confined qemu-system svirt_t "${uid}" "${procs}")
    passt=$(count_confined passt passt_t "${uid}" "${procs}")
    [ "${qemu}" -eq 1 ] ||
        fail "want exactly one qemu process as ${YOLOBOX_VM_USER} in svirt_t:s0:${CATEGORIES}, found ${qemu}"
    [ "${passt}" -ge 1 ] ||
        fail "no passt process as ${YOLOBOX_VM_USER} in passt_t:s0:${CATEGORIES}"
    ok "qemu runs as ${YOLOBOX_VM_USER} in svirt_t and ${passt} passt in passt_t, all in s0:${CATEGORIES}"
}

# ------------------------------------------------------------------- (c)

# What a domain's XML, read on stdin, says of itself: `instance=<marker or ->`,
# `ssh_port=<host port forwarded to 22, or ->` and `gaps=<what breaks the box's
# confinement, space separated, empty for a domain libvirt/domain.xml makes>`.
# python runs isolated (-I) and reads the XML with the stdlib alone.
domain_report() {
    python3 -I -c '
import sys
import xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
marker = root.find("metadata/{https://github.com/aka-rider/yolobox}instance")
print("instance=%s" % ((marker.text or "").strip() if marker is not None else "-"))
ranges = [r for r in root.findall("devices/interface/portForward/range") if r.get("to") == "22"]
print("ssh_port=%s" % (ranges[0].get("start") if ranges else "-"))
gaps = []
devices = root.find("devices")
for tag in ("graphics", "vsock", "filesystem", "channel", "hostdev", "tpm"):
    if devices is not None and devices.find(tag) is not None:
        gaps.append("device:" + tag)
backends = [
    (i.get("type"), i.find("backend").get("type") if i.find("backend") is not None else None)
    for i in root.findall("devices/interface")
]
if backends != [("user", "passt")]:
    gaps.append("interface")
for forward in root.findall("devices/interface/portForward"):
    if forward.get("address") != "127.0.0.1":
        gaps.append("portForward.address")
labels = {(l.get("type"), l.get("model")): l.get("relabel") for l in root.findall("seclabel")}
if labels.get(("dynamic", "selinux")) != "yes":
    gaps.append("seclabel.selinux")
if labels.get(("static", "dac")) != "yes":
    gaps.append("seclabel.dac")
print("gaps=%s" % " ".join(gaps))
'
}

# One line of a domain_report.
report_field() {
    sed -n "s/^$2=//p" <<<"$1"
}

check_domain_xml() {
    local xml report gaps instance
    xml=$(virsh_system dumpxml "${DOMAIN}") || fail "virsh dumpxml ${DOMAIN} failed"
    report=$(domain_report <<<"${xml}") || fail "could not read the XML of ${DOMAIN}"
    gaps=$(report_field "${report}" gaps)
    [ -z "${gaps}" ] || fail "${DOMAIN} breaks the box's confinement: ${gaps}"
    instance=$(report_field "${report}" instance)
    [ "${instance}" = "${NAME}" ] || fail "${DOMAIN} carries the metadata of '${instance}', not ${NAME}"
    SSH_PORT=$(report_field "${report}" ssh_port)
    [[ ${SSH_PORT} =~ ^[0-9]+$ ]] || fail "no forward to guest port 22 in the XML of ${DOMAIN}"
    ok "${DOMAIN} holds no device that reaches the host, one passt interface, both seclabels, forwards 127.0.0.1:${SSH_PORT} and carries the metadata"
}

# ------------------------------------------------------------------- (d)

# Prints "NAME PORT" for every other running domain tagged as a yolobox one.
running_peers() {
    local other xml names report port
    names=$(virsh_system list --name) || fail "virsh list failed"
    while IFS= read -r other; do
        if [ -z "${other}" ] || [ "${other}" = "${DOMAIN}" ]; then
            continue
        fi
        xml=$(virsh_system dumpxml "${other}") || fail "virsh dumpxml ${other} failed"
        # `|| return 1` as well: the substitution here sits in a list that
        # errexit ignores, so inherit_errexit alone would not stop it.
        report=$(domain_report <<<"${xml}") || return 1
        if [ "$(report_field "${report}" instance)" != - ]; then
            port=$(report_field "${report}" ssh_port)
            [[ ${port} =~ ^[0-9]+$ ]] || fail "no forward to guest port 22 in the XML of ${other}"
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
        expect_blocked "the ssh port of the running box ${peer_name}" "${YOLOBOX_GATEWAY}" "${peer_port}"
    done <<<"${peers}"
    if [ -z "${seen}" ]; then
        echo "SKIP peer"
    fi
}

check_tailnet() {
    local addrs tail_ip status=0 outcome
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
    # The canary: a host sshd that does not answer on the tailnet address
    # makes the guest's failure meaningless.
    if ! host_connects "${tail_ip}" 22; then
        echo "SKIP tailnet"
        return
    fi
    outcome=$(guest_tcp "${tail_ip}" 22)
    case ${outcome} in
    connected) echo "RESIDUAL tailnet-host-ssh" ;;
    timeout | refused) ok "the guest cannot reach the host's tailnet address on port 22 (${outcome})" ;;
    *) fail "the probe of the host's tailnet address ended neither connected nor blocked: ${outcome}" ;;
    esac
}

# The host table also rejects ::1, which passt maps the IPv6 gateway to.
check_v6_gateway() {
    local v6_gateway
    v6_gateway=$(guest_v6_gateway)
    if [ -z "${v6_gateway}" ] || [ "${CANARY6_PORT}" -eq 0 ]; then
        echo "SKIP v6-gateway"
        return
    fi
    expect_blocked "the IPv6 gateway, a listener of the host" "${v6_gateway}" "${CANARY6_PORT}"
}

# The host's own global IPv6 address is delivered over loopback, which the
# host table rejects.
check_v6_host() {
    local dev6 host_ip6=""
    read -r _ dev6 <<<"$(lan_default_route -6)"
    if [ -n "${dev6}" ]; then
        host_ip6=$(host_addr_on -6 "${dev6}")
    fi
    if [ -z "${host_ip6}" ] || [ -z "$(guest_v6_gateway)" ] || [ "${CANARY6_PORT}" -eq 0 ]; then
        echo "SKIP v6-host"
        return
    fi
    expect_blocked "the host's global IPv6 address, a listener of the host" "${host_ip6}" "${CANARY6_PORT}"
}

# $1 is the LAN's default gateway, or empty. The canary: a gateway whose port
# 80 does not answer the host makes the guest's failure meaningless.
check_lan_gateway() {
    local default_gw=$1
    if [ -z "${default_gw}" ] || ! host_connects "${default_gw}" 80; then
        echo "SKIP lan-gateway"
        return
    fi
    expect_blocked "the LAN's default gateway" "${default_gw}" 80
}

check_host_layer_alone() {
    local default_gw lan_dev host_ip outcome answer
    read -r default_gw lan_dev <<<"$(lan_default_route -4)"
    [ -n "${lan_dev}" ] || fail "the host has no IPv4 default route off the tailnet"
    host_ip=$(host_addr_on -4 "${lan_dev}")
    [ -n "${host_ip}" ] || fail "the host has no global IPv4 address on ${lan_dev}"

    TABLE_DELETED=1
    guest "sudo nft delete table ${EGRESS_TABLE}" || fail "could not delete the guest egress table"
    ok "the guest egress table is deleted; the host layer is all that is left"

    # The positive control: the same probe must report a connection where the
    # host layer allows one, or every "blocked" below would be the probe's own
    # failure.
    outcome=$(guest_tcp "${INTERNET_HOST}" "${INTERNET_PORT}")
    [ "${outcome}" = connected ] ||
        fail "the guest did not connect to ${INTERNET_HOST}:${INTERNET_PORT} with the host layer alone (${outcome}), so the probes below would prove nothing"
    ok "the guest connects to ${INTERNET_HOST}:${INTERNET_PORT}"

    start_canary
    expect_blocked "the gateway, the host's loopback, ssh" "${YOLOBOX_GATEWAY}" 22
    expect_blocked "the gateway, a listener of the host" "${YOLOBOX_GATEWAY}" "${CANARY_PORT}"
    expect_udp_blocked "the gateway, a listener of the host" "${YOLOBOX_GATEWAY}" "${CANARY_PORT}"
    check_v6_gateway
    expect_blocked "the host's primary address, ssh" "${host_ip}" 22
    expect_blocked "the host's primary address, a listener of the host" "${host_ip}" "${CANARY_PORT}"
    expect_udp_blocked "the host's primary address, a listener of the host" "${host_ip}" "${CANARY_PORT}"
    check_v6_host
    check_lan_gateway "${default_gw}"
    check_peers
    check_tailnet

    guest "curl -sSI --max-time 20 https://github.com >/dev/null" ||
        fail "the guest cannot fetch https://github.com with the host layer alone"
    ok "the guest reaches https://github.com"
    answer=$(guest "dig +short +time=5 +tries=1 @${YOLOBOX_GATEWAY} github.com") ||
        fail "dig @${YOLOBOX_GATEWAY} github.com failed in the guest"
    [ -n "${answer}" ] || fail "dig @${YOLOBOX_GATEWAY} github.com answered nothing"
    ok "the gateway answers DNS (UDP to the gateway's port 53 is the one host port the guest may use)"

    restore_egress_table || fail "could not restore the guest egress table"
    ok "the guest egress table is back"
}

# ------------------------------------------------------------------- (e)

check_forward_bound_to_loopback() {
    local listeners locals
    listeners=$(ss -ltnH "sport = :${SSH_PORT}") || fail "ss failed"
    locals=$(awk '{ print $4 }' <<<"${listeners}")
    [ -n "${locals}" ] || fail "nothing listens on port ${SSH_PORT}"
    [ "$(LC_ALL=C sort -u <<<"${locals}")" = "127.0.0.1:${SSH_PORT}" ] ||
        fail "port ${SSH_PORT} is bound beyond 127.0.0.1: ${locals}"
    ok "ssh port ${SSH_PORT} is bound on 127.0.0.1 only"
}

main() {
    local state
    state=$(virsh_system domstate "${DOMAIN}") || fail "no domain ${DOMAIN}: is the box created?"
    [ "${state}" = running ] || fail "${DOMAIN} is ${state}, not running"
    guest true || fail "cannot ssh to ${SSH_HOST} with ${SSH_CONFIG}"
    load_box_facts
    check_domain_label
    check_process_confinement
    check_domain_xml
    check_host_layer_alone
    check_forward_bound_to_loopback
    echo "box-libvirt-check: all checks passed"
}

main
