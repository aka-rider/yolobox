#!/usr/bin/env bash
# Acceptance check for the box-hardening change (nix/hardening.nix). Runs
# INSIDE a yolobox guest, as the operator (the account with passwordless
# sudo). The host driver tests/box-hardening-e2e.sh pipes it over `yo ssh`;
# a person inside the box can run it directly.
#
#   EXTRA_WRAPPERS="<wrapper>[=<caps>] ..." tests/box-hardening-check.sh
#
# EXTRA_WRAPPERS: space-separated setuid/capability wrappers a box adds
#   through yolobox.hardening.extraWrappers in its /etc/yolobox/local.nix.
#   Each is a name, or name=caps with the capability names getcap shows,
#   comma-separated, in any order (dumpcap=cap_net_raw,cap_setpcap). A bare
#   name means the wrapper carries none. A name of the allowlist is refused,
#   as the rebuild refuses it.
# EXTRA_AGENT_GROUPS: space-separated groups a box's local.nix adds the agent
#   to, beside users.
# TAILSCALE_VERSION: the version nix/pkgs/tailscale.nix pins; unset, the
#   box's tailscale is not compared (SKIP).
# ONLY=hardening | ONLY=compat: run just one half. Unset runs both. The
#   compat half ("nothing the agent needs broke") is the one to iterate on
#   against a box that is not hardened yet.
#
# The hardening half proves the box holds against the agent: sysctls, kernel
# parameters, blocked modules (sockets and link kinds), io_uring, the
# wrapper set and each wrapper's privilege, no setuid su, sudo closed to the
# agent, root and the agent locked, the agent's groups, /proc hiding every
# other account's pids from the agent, the 1Password proxy
# confined, sshd's settings, the tailscale pin, no forwarding, the gateway's
# ssh port closed to the agent and to its containers, both accounts refused
# off the gateway to the agent's forwarded 1Password agent (SKIP, and
# nothing tested, when that agent lists no key), the agent's own
# authorized_keys ignored by sshd's configuration, and tmpfiles refusing a
# symlink of the agent's into /etc. Its probes run as the agent WITHOUT the
# agent's login shell, since the agent edits that shell's rc files and PATH.
#
# The compat half proves nothing the agent needs broke: net-check, the kept
# qdisc, podman, chromium, a nix build, strace -p, the harness binaries, the
# herdr server's oom_score_adj, the agent's user manager, logind and the
# system bus answering the agent, rosetta on an aarch64 guest, the host OS stamp
# absent, the Lima guest agent running exactly when Lima's cidata ships it,
# `yolobox-guest rebuild` present, github.com's host key pinned.
#
# It stops at the first broken property and names it. There is no python in
# the box, so socket probes use perl and JSON uses jq.
set -Eeuo pipefail
# No globbing: the unquoted word lists below (wrapper names) must stay literal.
set -f

# Wrappers a hardened box keeps (nix/hardening.nix keptWrappers). Anything else in
# /run/wrappers/bin is an unexpected setuid/capability binary.
readonly WRAPPER_ALLOWLIST="sudo sudoedit unix_chkpwd newuidmap newgidmap"
readonly WRAPPERS=/run/wrappers/bin
readonly SYSBIN=/run/current-system/sw/bin
# The agent account (flake.nix agentUser, nix/base.nix) and the forwarded
# 1Password agent it reaches (nix/lib/agent-env.nix SSH_AUTH_SOCK).
readonly AGENT_USER=agent
readonly AGENT_UID=1000
readonly AGENT_AUTH_SOCK=/run/yolobox/op-agent.sock
readonly HK_NET=hk-test
# The addresses of the box's own network (nix/egress.nix, libvirt/domain.xml):
# the gateway is the host's loopback, the guest's address is enp0s1's.
readonly GATEWAY_ADDR=192.168.5.2
readonly GUEST_ADDR=192.168.5.15
# What sshd logs when AllowUsers refuses a name (openssh auth.c allowed_user).
readonly ALLOWUSERS_REFUSAL="not allowed because not listed in AllowUsers"
# The root-run command sshd reads the agent's keys from (nix/base.nix).
readonly AGENT_KEYS_COMMAND=/etc/ssh/agent-authorized-keys
# /proc's mount options: nixpkgs' three defaults, then the two
# nix/hardening.nix procOptions adds; gid=1 is wheel, the one group that sees
# every pid.
readonly PROC_OPTIONS="nosuid noexec nodev hidepid=invisible gid=1"

WORK=""
AGENT_HOME=""
SLEEPER_PID=""
NET_CREATED=""
PROBE_OUT=""
WRAPPER_CAPS=""
ZED_STAMP_DIR=""
EXTRA_NAMES=()

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

ok() {
    echo "ok: $*"
}

warn() {
    echo "WARN: $*" >&2
}

trap 'echo "FAIL: unexpected error at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

# Run a command as the agent, the way an AI session sees it. The compat half
# uses this one: "nothing broke" is about the agent's real environment.
#
# `sudo -iu agent` is a login shell, so the command gets the agent's real
# environment: PATH with ~/.local/bin (claude, pi) and
# /run/wrappers/bin, the nix-ld variables a vendor glibc binary needs, DISPLAY,
# DOCKER_HOST. A bare `sudo -u agent env PATH=...` would have to copy all of
# that by hand and drift. XDG_RUNTIME_DIR is the one thing the login shell does
# not set (no pam_systemd session), and rootless podman needs it.
#
# The agent's login shell is zsh, and sudo escapes every argument for it
# except `$`. So callers pass plain words only; anything with quotes or `$`
# lives in a file under $WORK and is passed by path.
as_agent() {
    sudo -n -iu "${AGENT_USER}" env "XDG_RUNTIME_DIR=/run/user/${AGENT_UID}" "$@"
}

# Run a command as the agent with nothing of the agent's own in the way. The
# hardening half uses this one: a probe through the login shell runs the
# agent's rc files and the agent's ~/.local/bin first, so an agent could make
# any refusal print. No shell, an environment of PATH and HOME alone, and
# callers name every binary by its ${SYSBIN} path. Arguments reach the command
# verbatim, so quotes and `$` are fine here.
as_agent_bare() {
    sudo -n -u "${AGENT_USER}" "${SYSBIN}/env" -i "PATH=${SYSBIN}" "HOME=${AGENT_HOME}" "$@"
}

cleanup() {
    local status=$?
    trap - ERR
    if [ -n "${SLEEPER_PID}" ]; then
        as_agent_bare "${SYSBIN}/kill" "${SLEEPER_PID}" 2>/dev/null || true # already gone is fine
    fi
    if [ -n "${NET_CREATED}" ]; then
        as_agent podman network rm -f "${HK_NET}" >/dev/null ||
            warn "cleanup: could not remove podman network ${HK_NET}"
    fi
    if [ -n "${ZED_STAMP_DIR}" ]; then
        restore_zed || warn "cleanup: ${AGENT_HOME}/.config/zed is NOT restored; move ${AGENT_HOME}/.config/zed.bak back by hand"
    fi
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
    exit "${status}"
}

# EXTRA_WRAPPERS, checked once: each word is a name, or name=caps, and no
# name is on the allowlist (the rebuild refuses that too: the table already
# fixes the privilege of those). Leaves the names in EXTRA_NAMES.
parse_extra_wrappers() {
    local extra name
    for extra in ${EXTRA_WRAPPERS:-}; do
        name=${extra%%=*}
        case ${extra} in
        *[!A-Za-z0-9._,=-]* | =* | *=) fail "EXTRA_WRAPPERS entry '${extra}' is not name or name=cap_a,cap_b" ;;
        esac
        case " ${WRAPPER_ALLOWLIST} " in
        *" ${name} "*) fail "EXTRA_WRAPPERS names ${name}, which is on the allowlist; the rebuild refuses that too" ;;
        esac
        EXTRA_NAMES+=("${name}")
    done
}

# The capabilities EXTRA_WRAPPERS states for a wrapper, sorted; "" when it
# states none or does not name the wrapper.
extra_caps_of() {
    local name=$1 extra
    for extra in ${EXTRA_WRAPPERS:-}; do
        case ${extra} in
        "${name}="*)
            sorted_caps <<<"${extra#*=}"
            return
            ;;
        esac
    done
    echo ""
}

# ---------------------------------------------------------------- hardening

check_sysctls() {
    local pair name want got
    # The sysctls nix/hardening.nix sets, plus kexec_load_disabled (from
    # security.protectKernelImage) and ptrace_scope, which must stay 0 (a
    # CONSTITUTION rule). The port floor is nix/hardening.nix's mkForce
    # statement: nixos-lima's 0 would let the agent bind port 22.
    for pair in \
        kernel.kptr_restrict=2 \
        kernel.unprivileged_bpf_disabled=1 \
        net.core.bpf_jit_harden=2 \
        kernel.io_uring_disabled=2 \
        dev.tty.ldisc_autoload=0 \
        dev.tty.legacy_tiocsti=0 \
        fs.protected_fifos=2 \
        fs.protected_regular=2 \
        fs.suid_dumpable=0 \
        kernel.kexec_load_disabled=1 \
        kernel.yama.ptrace_scope=0 \
        net.ipv4.conf.all.accept_redirects=0 \
        net.ipv4.conf.default.accept_redirects=0 \
        net.ipv4.conf.all.secure_redirects=0 \
        net.ipv4.conf.default.secure_redirects=0 \
        net.ipv4.conf.all.send_redirects=0 \
        net.ipv4.conf.default.send_redirects=0 \
        net.ipv4.conf.all.accept_source_route=0 \
        net.ipv4.conf.default.accept_source_route=0 \
        net.ipv6.conf.all.accept_redirects=0 \
        net.ipv6.conf.default.accept_redirects=0 \
        net.ipv6.conf.all.accept_source_route=0 \
        net.ipv6.conf.default.accept_source_route=0 \
        net.ipv4.ip_unprivileged_port_start=1024; do
        name=${pair%%=*}
        want=${pair#*=}
        got=$(sudo -n sysctl -n "${name}") || fail "sysctl ${name} is unreadable"
        [ "${got}" = "${want}" ] || fail "sysctl ${name} is ${got}, want ${want}"
    done
    ok "sysctls read back their hardened values"
}

# nix/base.nix forces networking.nat off, which is what keeps nixpkgs' nat
# module from turning forwarding on: nothing routes for others through the box.
check_ip_forward() {
    local got
    got=$(sudo -n sysctl -n net.ipv4.ip_forward) || fail "sysctl net.ipv4.ip_forward is unreadable"
    [ "${got}" = 0 ] || fail "net.ipv4.ip_forward is ${got}, want 0: the box routes"
    ok "net.ipv4.ip_forward is 0"
}

check_cmdline() {
    local cmdline param
    cmdline=$(cat /proc/cmdline)
    # The first three are boot.kernelParams in nix/hardening.nix (a consistency
    # test compares them); nohibernate comes from security.protectKernelImage.
    for param in slab_nomerge page_alloc.shuffle=1 debugfs=off nohibernate; do
        grep -qxF -- "${param}" <<<"${cmdline// /$'\n'}" ||
            fail "kernel parameter ${param} is missing from /proc/cmdline: ${cmdline}"
        ok "kernel parameter ${param} is on the command line"
    done
}

# Stage the helper files the agent-side checks need. Plain files, so no
# quoting crosses sudo and the zsh login shell.
stage_helpers() {
    WORK=$(mktemp -d /tmp/hk-check.XXXXXX)
    chmod 755 "${WORK}"

    # Probe exit codes (see agent_probe): 0 the kernel refused, 3 it did not.
    cat >"${WORK}/socket.pl" <<'EOF'
use Socket;
my ($domain, $type, $proto) = @ARGV;
if (socket(my $s, $domain, $type, $proto)) { print "opened\n"; exit 3; }
print "refused: $!\n";
exit 0;
EOF

    # io_uring_setup is syscall 425 on aarch64 and on the x86_64 table. The
    # kernel params struct is 120 bytes. kernel.io_uring_disabled=2 answers
    # EPERM before it reads the struct.
    cat >"${WORK}/iouring.pl" <<'EOF'
my $params = "\0" x 120;  # syscall() needs a writable buffer
my $fd = syscall(425, 1, $params);
if ($fd == -1 && $!{EPERM}) { print "refused: $!\n"; exit 0; }
print "io_uring_setup returned $fd ($!)\n";
exit 3;
EOF

    # Probe exit codes (see agent_probe): 0 no TCP connection, 3 it connected.
    # Any failure to connect counts, a reject, a drop (the 5 s alarm) or a
    # timeout alike.
    cat >"${WORK}/tcp.pl" <<'EOF'
use Socket;
my ($host, $port) = @ARGV;
socket(my $s, PF_INET, SOCK_STREAM, 0) or die "socket: $!\n";
my $connected = eval {
    local $SIG{ALRM} = sub { die "timed out\n" };
    alarm 5;
    my $ok = connect($s, sockaddr_in($port, inet_aton($host)));
    alarm 0;
    die "$!\n" unless $ok;
    1;
};
if ($connected) { print "connected\n"; exit 3; }
print "no connection: $@";
exit 0;
EOF

    # Runs as root of the agent's own user and network namespace (unshare -Urn).
    # fq_codel must attach (it is the one qdisc that stays), then a u32
    # classifier on it must be refused by the kernel. Exits 0 on that refusal,
    # 3 when the classifier attached, 4 when fq_codel did not, 5 when the
    # refusal is not "classifier not found" (so unshare or tc broke instead).
    cat >"${WORK}/netsched.sh" <<'EOF'
tc qdisc add dev lo root handle 1: fq_codel || exit 4
if out=$(tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dst 0.0.0.0/0 flowid 1:1 2>&1); then
    echo "u32 attached"
    exit 3
fi
case $out in
*"TC classifier not found"*) echo "refused: $out"; exit 0 ;;
*) echo "$out"; exit 5 ;;
esac
EOF

    # The strace target: a reader of the fifo, which blocks in open() until
    # this script writes to it. Starts it detached and prints its pid.
    mkfifo -m 644 "${WORK}/fifo"
    cat >"${WORK}/sleeper.sh" <<EOF
#!/usr/bin/env bash
cat "${WORK}/fifo" </dev/null >/dev/null 2>&1 &
echo \$!
EOF
    chmod 755 "${WORK}/sleeper.sh"

    stage_zed_swap

    # The name is unique per run, so the build is never a cache hit.
    cat >"${WORK}/build.nix" <<EOF
derivation {
  name = "hk-$$-$(date +%s)";
  builder = "/bin/sh";
  args = [ "-c" "echo ok > \$out" ];
  system = builtins.currentSystem;
}
EOF
}

# Runs a perl probe as the agent and leaves its output in PROBE_OUT. A probe
# exits 0 when the kernel refused, 3 when the hardening did not hold. Any other
# status means sudo or perl broke, which says nothing about the hardening, so
# it gets a different message.
agent_probe() {
    local property_failed=$1 status=0
    shift
    PROBE_OUT=$(as_agent_bare "${SYSBIN}/perl" "$@" 2>&1) || status=$?
    case ${status} in
    0) ;;
    3) fail "${property_failed}: ${PROBE_OUT}" ;;
    *) fail "could not run the probe 'perl $*' as the agent (sudo or perl exited ${status}): ${PROBE_OUT}" ;;
    esac
}

# An agent-side socket(2) must fail: the kernel would otherwise autoload the
# protocol module (net-pf-N alias) for an unprivileged process.
check_socket_refused() {
    local label=$1 module=$2 domain=$3 type=$4 proto=$5 mods
    agent_probe "agent could open a ${label} socket (${module} autoloads)" \
        "${WORK}/socket.pl" "${domain}" "${type}" "${proto}"
    mods=$(lsmod)
    if grep -q "^${module} " <<<"${mods}"; then
        fail "${module} is loaded in the kernel"
    fi
    ok "${label} socket refused for the agent (${PROBE_OUT}), ${module} not loaded"
}

# The sysctl read-back proves the setting; this proves the agent really meets it.
check_io_uring() {
    agent_probe "io_uring_setup was not refused with EPERM for the agent" "${WORK}/iouring.pl"
    ok "io_uring_setup ${PROBE_OUT} for the agent"
}

# modprobe -n -v NAME must print the install rule that routes NAME to bin/false.
check_modprobe_routed() {
    local module=$1 out
    out=$(modprobe -n -v "${module}" 2>&1) || fail "modprobe -n -v ${module} failed: ${out}"
    case ${out} in
    *install*bin/false*) ;;
    *) fail "modprobe -n -v ${module} does not hit the install rule (want install ... bin/false): ${out}" ;;
    esac
}

check_modules() {
    # AF_BLUETOOTH=31, SOCK_STREAM=1, BTPROTO_RFCOMM=3
    check_socket_refused bluetooth bluetooth 31 1 3
    # AF_INET=2, SOCK_STREAM=1, IPPROTO_SCTP=132
    check_socket_refused sctp sctp 2 1 132
    # AF_PPPOX=24, SOCK_STREAM=1, PX_PROTO_PPTP=2
    check_socket_refused pptp pptp 24 1 2
    # AF_MCTP=45, SOCK_DGRAM=2
    check_socket_refused mctp mctp 45 2 0
    # AF_NETLINK=16, SOCK_RAW=3, NETLINK_ISCSI=8 and NETLINK_XFRM=6
    check_socket_refused iscsi scsi_transport_iscsi 16 3 8
    check_socket_refused xfrm xfrm_user 16 3 6
    # AF_INET=2, SOCK_DGRAM=2, IPPROTO_L2TP=115
    check_socket_refused l2tp_ip l2tp_core 2 2 115
    check_modprobe_routed bluetooth
    ok "explicit modprobe of bluetooth is routed to bin/false"
}

# `ip link add ... type KIND` in the agent's own namespace asks the kernel for
# the module rtnl-link-KIND, before the kernel looks at the kind's options.
# The blocked kinds must be refused as unsupported, with their module not
# loaded; the positive control is a bridge, which stays, and must attach.
# `kind|module|parent|options`, one per word: a kind on top of another device
# (macsec, ipvlan) takes `link PARENT` before the name, where iproute2 reads
# it; after `type KIND` every word is the kind's own option, and `link` there
# is a usage error (`macsec: unknown command "link"?`), not the kernel's
# refusal. hsr's slaves are its own options.
check_link_kinds() {
    local entry kind module parent options out loaded
    as_agent_bare "${SYSBIN}/unshare" -Urn "${SYSBIN}/ip" link add b0 type bridge ||
        fail "the agent could not add a bridge in its own namespace, so a refused link kind would prove nothing"
    ok "the agent can add a bridge in its own namespace"
    for entry in \
        "vxlan|vxlan||id 1 dstport 4789" \
        "geneve|geneve||id 1 remote 192.0.2.1" \
        "bareudp|bareudp||dstport 6635 ethertype ipv4" \
        "gtp|gtp||role sgsn" \
        "hsr|hsr||slave1 lo slave2 lo" \
        "macsec|macsec|lo|" \
        "bond|bonding||" \
        "team|team||" \
        "ipvlan|ipvlan|lo|mode l2"; do
        IFS='|' read -r kind module parent options <<<"${entry}"
        # ${options} is split on purpose: it is ip's own word list.
        # shellcheck disable=SC2086
        if out=$(as_agent_bare "${SYSBIN}/unshare" -Urn "${SYSBIN}/ip" link add ${parent:+link "${parent}"} l0 type "${kind}" ${options} 2>&1); then
            fail "the agent added a ${kind} link in its own namespace (${module} autoloads)"
        fi
        case ${out} in
        *"Operation not supported"* | *"Unknown device type"*) ;;
        *) fail "ip link add type ${kind} failed, but not with the kernel refusing the kind: ${out}" ;;
        esac
        loaded=$(lsmod | awk -v m="${module}" '$1 == m { print $1 }')
        [ -z "${loaded}" ] || fail "${module} is loaded in the kernel after the ${kind} refusal"
        ok "the agent cannot add a ${kind} link (${out}), ${module} not loaded"
    done
}

# The agent is root of its own user and network namespace and can attach
# traffic-control objects there, which autoloads the module behind each one.
# The net/sched modules are blocked (nix/hardening.nix), except sch_fq_codel.
check_net_sched() {
    local out status=0 loaded
    if out=$(as_agent_bare "${SYSBIN}/unshare" -Urn "${SYSBIN}/tc" qdisc add dev lo root netem delay 1ms 2>&1); then
        fail "the agent attached a netem qdisc in its own namespace (sch_netem autoloads)"
    fi
    case ${out} in
    *"Specified qdisc kind is unknown"*) ;;
    *) fail "tc qdisc add netem failed, but not with the kernel refusing the qdisc kind: ${out}" ;;
    esac
    ok "the agent cannot attach a netem qdisc (${out})"
    out=$(as_agent_bare "${SYSBIN}/unshare" -Urn "${SYSBIN}/bash" "${WORK}/netsched.sh" 2>&1) || status=$?
    case ${status} in
    0) ;;
    3) fail "the agent attached a u32 classifier in its own namespace (cls_u32 autoloads): ${out}" ;;
    4) fail "fq_codel did not attach in the agent's namespace, so the kept qdisc is broken: ${out}" ;;
    *) fail "netsched.sh exited ${status}, not with the kernel refusing the classifier: ${out}" ;;
    esac
    ok "the agent cannot attach a u32 classifier (${out})"
    loaded=$(lsmod | awk '$1 ~ /^(cls|act|sch)_/ && $1 != "sch_fq_codel" {print $1}')
    [ -z "${loaded}" ] || fail "net/sched modules are loaded: ${loaded//$'\n'/ }"
    check_modprobe_routed cls_u32
    ok "no cls_, act_ or sch_ module is loaded but sch_fq_codel, and cls_u32 is routed to bin/false"
}

# Capability names, one per line or comma separated on stdin, as one sorted
# comma-separated list without duplicates: the spelling nix/hardening.nix
# compares in. Nix sorts bytewise, so the sort must not use the box's locale:
# under en_US.UTF-8 cap_syslog sorts before cap_sys_time.
sorted_caps() {
    tr ',' '\n' | LC_ALL=C sort -u | paste -sd, -
}

# Leaves the capability names a wrapper carries in WRAPPER_CAPS, sorted; ""
# when it carries none. getcap prints "path cap_x,cap_y=ep" or "path =
# cap_x,cap_y+ep" by libcap version, and nothing for a file without
# capabilities. Any other text fails: "path =ep" (no names) grants EVERY
# capability, and so does "all=ep".
read_wrapper_caps() {
    local path=$1 out rest
    out=$(getcap "${path}") || fail "getcap ${path} failed"
    rest=${out#"${path}"}
    rest=${rest# }
    if [ -z "${rest}" ]; then
        WRAPPER_CAPS=""
        return
    fi
    [[ ${rest} =~ ^(=\ )?(cap_[a-z_]+(,cap_[a-z_]+)*)[=+][eip]+$ ]] ||
        fail "getcap ${path} printed a capability text this check does not read, so it may grant every capability: '${out}'"
    WRAPPER_CAPS=$(sorted_caps <<<"${BASH_REMATCH[2]}")
}

check_wrappers() {
    local allowlist have want name path caps expected_caps stat owner group mode
    read -ra allowlist <<<"${WRAPPER_ALLOWLIST}"
    # The trailing slash: /run/wrappers/bin is a symlink to the generation's
    # directory, and find does not follow its start point without it.
    have=$(find "${WRAPPERS}/" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)
    [ -n "${have}" ] || fail "found no wrappers under ${WRAPPERS}, so the wrapper set cannot be checked"
    want=$(printf '%s\n' "${allowlist[@]}" "${EXTRA_NAMES[@]}" | LC_ALL=C sort -u)
    [ "${have}" = "${want}" ] || fail "${WRAPPERS} differs from the allowlist (< unexpected, > missing)
$(diff <(printf '%s\n' "${have}") <(printf '%s\n' "${want}") || true)"
    ok "wrapper set is exactly: ${want//$'\n'/ }"

    # Each wrapper's privilege, as nix/hardening.nix states it: root's, never
    # setgid, setuid exactly when it carries no capability, and sudo's pair
    # closed to anyone outside wheel (security.sudo.execWheelOnly).
    for name in ${have}; do
        path="${WRAPPERS}/${name}"
        read_wrapper_caps "${path}"
        caps=${WRAPPER_CAPS}
        case ${name} in
        # nixpkgs grants cap_setpcap beside the one the name suggests
        # (getcap: "newuidmap cap_setuid,cap_setpcap=ep").
        newuidmap) expected_caps=cap_setpcap,cap_setuid ;;
        newgidmap) expected_caps=cap_setgid,cap_setpcap ;;
        *) expected_caps=$(extra_caps_of "${name}") ;;
        esac
        [ "${caps}" = "${expected_caps}" ] ||
            fail "wrapper ${name} has capabilities '${caps}', want '${expected_caps}'"
        stat=$(stat -c '%U %G %a' "${path}") || fail "cannot stat ${path}"
        read -r owner group mode <<<"${stat}"
        [ "${owner}" = root ] || fail "wrapper ${name} is owned by ${owner}, not root"
        [ ! -g "${path}" ] || fail "wrapper ${name} is setgid"
        if [ -z "${caps}" ]; then
            [ -u "${path}" ] || fail "wrapper ${name} carries no capability and is not setuid: it is no wrapper"
        else
            [ ! -u "${path}" ] || fail "wrapper ${name} is setuid on top of its capabilities"
        fi
        case ${name} in
        sudo | sudoedit)
            [ "${group}" = wheel ] || fail "${name} is group ${group}, not wheel"
            [ $((8#${mode} & 1)) -eq 0 ] || fail "${name} is mode ${mode}: executable by others, so the agent can run it"
            ;;
        esac
    done
    ok "every wrapper is root's, none setgid, setuid exactly when it carries no capability, and sudo's pair is wheel-only"
}

# The store's plain `su` stays on the system path; without a setuid bit it
# cannot do anything, and the allowlist above holds no su wrapper.
check_no_su() {
    local resolved
    if [ ! -e "${SYSBIN}/su" ]; then
        ok "su is not on the system path"
        return
    fi
    resolved=$(readlink -f "${SYSBIN}/su")
    [ ! -u "${resolved}" ] || fail "the system's su (${resolved}) is setuid"
    [ ! -g "${resolved}" ] || fail "the system's su (${resolved}) is setgid"
    ok "the system's su (${resolved}) runs with the caller's own rights"
}

# The mode check above says sudo is not executable by others; this is the
# agent trying anyway.
check_sudo_refuses_agent() {
    local out status=0
    out=$(as_agent_bare "${WRAPPERS}/sudo" -n true 2>&1) || status=$?
    [ "${status}" -ne 0 ] || fail "the agent ran sudo -n true"
    case ${out} in
    *"Permission denied"*) ;;
    *) fail "sudo -n true as the agent failed (exit ${status}), but not with Permission denied: ${out}" ;;
    esac
    ok "the agent cannot run sudo (${out})"
}

# nix/base.nix locks root and the agent on every activation, and keeps the
# agent out of every supplementary group (systemd-journal would show it the
# operator's sudo lines).
check_accounts_locked() {
    local account entry field groups want
    for account in root "${AGENT_USER}"; do
        entry=$(sudo -n getent shadow "${account}") || fail "no shadow entry for ${account}"
        field=${entry#*:}
        field=${field%%:*}
        case ${field} in
        '!'* | '*'*) ;;
        *) fail "the password of ${account} is not locked (shadow field '${field}')" ;;
        esac
    done
    ok "root and ${AGENT_USER} have locked passwords"
    groups=$(id -Gn "${AGENT_USER}" | tr ' ' '\n' | LC_ALL=C sort -u | paste -sd' ' -) || fail "id -Gn ${AGENT_USER} failed"
    want=$(tr ' ' '\n' <<<"users ${EXTRA_AGENT_GROUPS:-}" | grep . | LC_ALL=C sort -u | paste -sd' ' -)
    [ "${groups}" = "${want}" ] || fail "${AGENT_USER} is in groups '${groups}', want '${want}' (users plus EXTRA_AGENT_GROUPS)"
    [ "$(id -u "${AGENT_USER}")" = "${AGENT_UID}" ] || fail "${AGENT_USER} is not uid ${AGENT_UID}"
    ok "${AGENT_USER} is uid ${AGENT_UID} in the groups: ${want}"
}

# /proc is mounted with hidepid (nix/hardening.nix procOptions), so the agent
# sees no pid but its own: not root's, not the operator's. The mount is read
# back whole first, because a lost nosuid or gid= is as much a regression as
# a lost hidepid. Then the agent is made to look: at pid 1, at this operator
# shell, and at the whole table through ps.
check_hidepid() {
    local options opt pid out users
    options=$(findmnt -no OPTIONS /proc) || fail "findmnt -no OPTIONS /proc failed: the mount cannot be read back"
    [ -n "${options}" ] || fail "findmnt printed no options for /proc"
    for opt in ${PROC_OPTIONS}; do
        grep -qxF -- "${opt}" <<<"${options//,/$'\n'}" || fail "/proc is mounted without ${opt}: ${options}"
    done
    ok "/proc is mounted with ${PROC_OPTIONS// /,}"
    for pid in 1 $$; do
        if out=$(as_agent_bare "${SYSBIN}/ls" "/proc/${pid}" 2>&1); then
            fail "the agent listed /proc/${pid} (${out//$'\n'/ })"
        fi
        case ${out} in
        *"No such file or directory"*) ;;
        *) fail "ls /proc/${pid} as the agent failed, but not with ENOENT, so the pid is visible and only unreadable: ${out}" ;;
        esac
    done
    users=$(as_agent_bare "${SYSBIN}/ps" -eo user= | awk '{ print $1 }' | LC_ALL=C sort -u | paste -sd' ' -) ||
        fail "ps -eo user failed as the agent"
    [ "${users}" = "${AGENT_USER}" ] || fail "ps as the agent lists the processes of '${users}', want ${AGENT_USER} alone"
    ok "the agent sees neither /proc/1 nor /proc/$$ (the operator's shell), and ps shows it ${AGENT_USER}'s processes alone"
}

# The forwarded 1Password socket reaches the agent through a proxy unit
# (nix/base.nix, docs/ssh.md) that must hold nothing but the two sockets: no
# writable system, no homes, no network, unix sockets only. The positive
# control is that the agent still reaches the agent behind it: ssh-add -L
# exits 0 with keys, 1 with none, and 2 when the socket is dead.
check_proxy_confined() {
    local unit=yolobox-op-agent.service props want got keys status=0
    props=$(systemctl show -p ProtectSystem,ProtectHome,PrivateNetwork,RestrictAddressFamilies "${unit}") ||
        fail "could not read the confinement of ${unit}"
    for want in ProtectSystem=strict ProtectHome=yes PrivateNetwork=yes RestrictAddressFamilies=AF_UNIX; do
        got=$(sed -n "s/^${want%%=*}=//p" <<<"${props}")
        [ "${got}" = "${want#*=}" ] || fail "${unit} has ${want%%=*}=${got}, want ${want#*=}"
    done
    keys=$(as_agent_bare "SSH_AUTH_SOCK=${AGENT_AUTH_SOCK}" "${SYSBIN}/ssh-add" -L 2>&1) || status=$?
    [ "${status}" -le 1 ] || fail "ssh-add -L through the confined ${unit} exited ${status}: ${keys}"
    ok "${unit} is confined (strict, no homes, no network, AF_UNIX) and the agent still reaches the forwarded ssh agent"
}

# sshd's effective settings: sftp by the in-process server (no sftp-server
# binary to run), and no root login at all. Case-insensitive: OpenSSH 10
# prints the keywords in their canonical case (`Subsystem sftp internal-sftp`),
# older ones in lowercase.
check_sshd_settings() {
    local config setting
    config=$(sudo -n sshd -T) || fail "sshd -T failed"
    for setting in "subsystem sftp internal-sftp" "permitrootlogin no"; do
        grep -qix -- "${setting}" <<<"${config}" || fail "sshd -T lacks '${setting}'"
    done
    ok "sshd runs internal-sftp and permits no root login"
}

# The box runs the tailscale nix/pkgs/tailscale.nix pins. The pin's version
# comes in TAILSCALE_VERSION (the e2e driver reads it from the checkout);
# unset, nothing is compared.
check_tailscale_version() {
    local got
    if [ -z "${TAILSCALE_VERSION:-}" ]; then
        echo "SKIP: tailscale's version was NOT compared: TAILSCALE_VERSION is unset" >&2
        return
    fi
    got=$(tailscale version | head -n 1) || fail "tailscale version failed"
    [ "${got}" = "${TAILSCALE_VERSION}" ] || fail "tailscale is ${got}, the pin says ${TAILSCALE_VERSION}"
    ok "tailscale is the pinned ${TAILSCALE_VERSION}"
}

# podman as the agent, with the runtime directory rootless podman needs.
agent_podman() {
    as_agent_bare "XDG_RUNTIME_DIR=/run/user/${AGENT_UID}" "${SYSBIN}/podman" "$@"
}

# A rootless container's traffic is the agent's, so the egress table must
# refuse it the gateway's ssh port, through pasta and on the host network
# alike. The positive control is the same nc reaching the internet.
check_container_egress() {
    local network out
    agent_podman run --rm docker.io/library/busybox nc -z -w 3 github.com 443 ||
        fail "a container cannot reach github.com:443, so a refused gateway would prove nothing"
    ok "a rootless container reaches github.com:443"
    for network in "" --network=host; do
        # ${network} is one option or nothing; an empty word would be an image name.
        # shellcheck disable=SC2086
        if out=$(agent_podman run --rm ${network} docker.io/library/busybox nc -z -w 3 "${GATEWAY_ADDR}" 22 2>&1); then
            fail "a rootless container${network:+ on ${network}} connected to ${GATEWAY_ADDR}:22"
        fi
        ok "a rootless container${network:+ on ${network}} cannot reach ${GATEWAY_ADDR}:22 (${out})"
    done
}

# Puts the agent's ~/.config/zed aside and points the name at /etc, or
# restores it. Run as the agent; STAMPDIR records whether the directory
# existed, so a box that never had one gets none back.
stage_zed_swap() {
    cat >"${WORK}/zed-swap.sh" <<'EOF'
set -eu
stampdir=$1 action=$2
zed=~/.config/zed
case ${action} in
swap)
    mkdir -p ~/.config
    if [ -e "${zed}" ] || [ -L "${zed}" ]; then mv "${zed}" "${zed}.bak"; touch "${stampdir}/had-zed"; fi
    ln -s /etc "${zed}"
    ;;
restore)
    rm "${zed}"
    if [ -e "${stampdir}/had-zed" ]; then mv "${zed}.bak" "${zed}"; fi
    ;;
esac
EOF
}

restore_zed() {
    as_agent_bare "${SYSBIN}/bash" "${WORK}/zed-swap.sh" "${ZED_STAMP_DIR}" restore || return 1
    as_agent_bare "${SYSBIN}/rm" -rf "${ZED_STAMP_DIR}" || return 1
    ZED_STAMP_DIR=""
}

# The tmpfiles rules that link files into the agent's home (nix/lib/
# home-tmpfiles.nix) run as root. With ~/.config/zed pointed at /etc by the
# agent, systemd-tmpfiles must refuse the path (an unsafe transition from
# the agent's home into root's directory) rather than write
# /etc/settings.json. The positive control is the same run passing once the
# directory is back.
check_tmpfiles_refuses_agent_symlink() {
    local out status=0
    ZED_STAMP_DIR=$(as_agent_bare "${SYSBIN}/mktemp" -d /tmp/hk-zed.XXXXXX) ||
        fail "could not create a directory as the agent"
    as_agent_bare "${SYSBIN}/bash" "${WORK}/zed-swap.sh" "${ZED_STAMP_DIR}" swap ||
        fail "could not point ${AGENT_HOME}/.config/zed at /etc as the agent"
    out=$(sudo -n systemd-tmpfiles --create 2>&1) || status=$?
    [ "${status}" -ne 0 ] || fail "systemd-tmpfiles --create succeeded with ${AGENT_HOME}/.config/zed pointing at /etc"
    case ${out} in
    *"unsafe path transition"*) ;;
    *) fail "systemd-tmpfiles --create failed (exit ${status}), but not on the unsafe path transition: ${out}" ;;
    esac
    [ ! -e /etc/settings.json ] || fail "/etc/settings.json exists: the agent's symlink steered a root-run rule into /etc"
    restore_zed || fail "could not restore ${AGENT_HOME}/.config/zed"
    out=$(sudo -n systemd-tmpfiles --create 2>&1) ||
        fail "systemd-tmpfiles --create fails with ${AGENT_HOME}/.config/zed restored: ${out}"
    ok "systemd-tmpfiles refuses the agent's symlink into /etc and passes once the directory is back"
}

# The gateway is the host's own loopback, so a connection to its port 22 would
# reach the host's sshd. The egress table must refuse it for the agent.
check_gateway_ssh_closed() {
    agent_probe "the agent connected to ${GATEWAY_ADDR}:22, the host's loopback" \
        "${WORK}/tcp.pl" "${GATEWAY_ADDR}" 22
    ok "the agent cannot reach ${GATEWAY_ADDR}:22 (${PROBE_OUT})"
}

# ssh as the agent, with the forwarded agent and nothing of the agent's own:
# no ~/.ssh/config, no known hosts. The target's identity is not the question.
agent_ssh() {
    as_agent_bare "SSH_AUTH_SOCK=${AGENT_AUTH_SOCK}" "${SYSBIN}/ssh" -F /dev/null \
        -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 "$@"
}

# The journal cursor after sshd's last line, so a later read sees only what
# the probes caused. sshd has logged at least this session's login.
sshd_journal_cursor() {
    local cursor
    cursor=$(sudo -n journalctl -u sshd.service -n 1 -q -o cat --show-cursor | sed -n 's/^-- cursor: //p')
    [ -n "${cursor}" ] || fail "sshd.service has no journal entry to read on from"
    echo "${cursor}"
}

# sshd's lines since the cursor. --sync returns once every line logged before
# the call is in the journal, so this needs no wait.
sshd_journal_since() {
    sudo -n journalctl --sync || fail "journalctl --sync failed"
    sudo -n journalctl -u sshd.service --after-cursor="$1" -q -o cat
}

# Both accounts log in only from the gateway (nix/base.nix AllowUsers), the
# one address the agent cannot claim off enp0s1. On a Linux host the agent's
# forwarded 1Password agent serves the operator's key, which is also the key
# the agent account is authorized with, so from inside the box that key must
# open nothing: neither account, at loopback or at the guest's own address.
#
# A Permission denied alone proves nothing: ssh fails the same way when the
# forwarded agent offers no key the box authorizes, and nothing the box admits
# is reachable from inside it, so there is no login to use as a positive
# control. The proof is sshd's own journal: each probe must be refused, and
# sshd must have logged that AllowUsers refused that name from that address.
# openssh judges AllowUsers before any key, so any other outcome is a FAIL.
# The probes carry the forwarded key so that the refusal is of the key that
# opens the box from the host; with no key forwarded they print a SKIP. An
# agent that cannot be reached at all is a FAIL.
#
# sshd penalises an address that fails authentication (PerSourcePenalties,
# enforced once the address accumulates 15 s, 5 s per failure), and only the
# gateway is exempt. So each address below takes exactly two failures per
# run, one per account; a penalised address shows as a connection closed
# before authentication, not a refusal.
# $1 is the operator's account name.
check_logins_refused_off_gateway() {
    local operator=$1 user addr out keys keys_status=0 cursor journal
    # ssh-add -L exits 1 when the agent lists no identity and 2 when it cannot
    # reach the agent; only the first is a SKIP.
    keys=$(as_agent_bare "SSH_AUTH_SOCK=${AGENT_AUTH_SOCK}" "${SYSBIN}/ssh-add" -L 2>&1) || keys_status=$?
    case ${keys_status} in
    0) ;;
    1)
        echo "SKIP: sshd's refusal of ${operator} and ${AGENT_USER} off the gateway was NOT tested: the agent's forwarded ssh agent lists no key (ssh-add -L: ${keys}). Connect with an ssh agent forwarded and run this again" >&2
        return
        ;;
    *) fail "ssh-add -L exited ${keys_status}: the agent's ssh agent socket is unreachable (${keys})" ;;
    esac
    cursor=$(sshd_journal_cursor)
    for user in "${operator}" "${AGENT_USER}"; do
        for addr in 127.0.0.1 "${GUEST_ADDR}"; do
            if out=$(agent_ssh "${user}@${addr}" true 2>&1); then
                fail "the agent logged in as ${user} at ${addr} with its forwarded agent"
            fi
            case ${out} in
            *"Permission denied"*) ;;
            *) fail "ssh as ${user} at ${addr} failed, but not with Permission denied (a penalised address closes the connection instead): ${out}" ;;
            esac
            journal=$(sshd_journal_since "${cursor}")
            grep -qF "User ${user} from ${addr} ${ALLOWUSERS_REFUSAL}" <<<"${journal}" ||
                fail "sshd refused ${user} at ${addr}, but its journal does not say AllowUsers did: ${journal}"
            ok "sshd refuses ${user} at ${addr} because of AllowUsers"
        done
    done
}

# The rule behind the AllowUsers entry: the agent cannot claim the gateway's
# address over loopback. -C matches the rule by meaning; `iptables -S` prints
# its options in another order than the one the unit adds them in.
check_gateway_spoof_dropped() {
    sudo -n iptables -C nixos-fw ! -i enp0s1 -s "${GATEWAY_ADDR}" -j DROP ||
        fail "nixos-fw has no rule dropping ${GATEWAY_ADDR} off enp0s1: $(sudo -n iptables -S nixos-fw)"
    ok "nixos-fw drops packets claiming ${GATEWAY_ADDR} on any interface but enp0s1"
}

# ~agent/.ssh/authorized_keys is the agent's to write, so sshd must not read
# it: nix/base.nix gives the agent AuthorizedKeysFile none and a root-run
# AuthorizedKeysCommand that reads the operator's root-owned file. No login
# from inside the box gets as far as a key, since AllowUsers refuses every
# address the agent can come from first (check_logins_refused_off_gateway),
# so a planted key that is refused would prove nothing. The proof is sshd's
# effective configuration for the agent arriving from the gateway (sshd -T
# -C), and the command being root's and writable by nobody else.
check_agent_authorized_keys_ignored() {
    local config setting owner_mode
    config=$(sudo -n sshd -T -C "user=${AGENT_USER},addr=${GATEWAY_ADDR},host=${GATEWAY_ADDR}") ||
        fail "sshd -T -C for ${AGENT_USER} from ${GATEWAY_ADDR} failed"
    for setting in "authorizedkeysfile none" "authorizedkeyscommand ${AGENT_KEYS_COMMAND}" "authorizedkeyscommanduser root"; do
        grep -qix -- "${setting}" <<<"${config}" ||
            fail "sshd -T for ${AGENT_USER} from ${GATEWAY_ADDR} lacks '${setting}': $(grep -i '^authorizedkeys' <<<"${config}")"
    done
    owner_mode=$(stat -c '%U %a' "${AGENT_KEYS_COMMAND}") || fail "cannot stat ${AGENT_KEYS_COMMAND}"
    [ "${owner_mode}" = "root 555" ] || fail "${AGENT_KEYS_COMMAND} is ${owner_mode}, want root 555"
    ok "sshd reads the agent's keys from root's ${AGENT_KEYS_COMMAND} and from no authorized_keys file"
}

hardening_checks() {
    check_sysctls
    check_ip_forward
    check_cmdline
    check_modules
    check_link_kinds
    check_net_sched
    check_io_uring
    check_wrappers
    check_no_su
    check_sudo_refuses_agent
    check_accounts_locked
    check_hidepid
    check_proxy_confined
    check_sshd_settings
    check_tailscale_version
    check_gateway_ssh_closed
    check_container_egress
    check_logins_refused_off_gateway "$(id -un)"
    check_gateway_spoof_dropped
    check_agent_authorized_keys_ignored
    check_tmpfiles_refuses_agent_symlink
}

# ------------------------------------------------------------ nothing broke

check_net() {
    local out
    out=$(yolobox-guest net-check) || fail "yolobox-guest net-check exited nonzero: ${out}"
    grep -qx 'egress=ok' <<<"${out}" || fail "net-check: want egress=ok, got: ${out}"
    ok "net-check: egress=ok"
}

# The guest gates each host difference on the fact it needs, so nothing tells
# it which OS runs it (nix/base.md, "No host OS in the guest"). A box built
# before that carried the stamp; a rebuild removes it.
check_no_host_os_stamp() {
    [ ! -e /etc/yolobox/host-os ] ||
        fail "/etc/yolobox/host-os exists: the box was built by a flake that told it its host OS"
    ok "the box holds no host OS stamp"
}

# nixos-lima links /etc/environment into root's 0700 cidata mount, which every
# user manager then fails to read (nix/base.md, "/etc/environment").
check_etc_environment() {
    if [ -L /etc/environment ] || [ -e /etc/environment ]; then
        cat /etc/environment >/dev/null ||
            fail "/etc/environment exists but the operator cannot read it"
    fi
    ok "/etc/environment is absent or readable"
}

# The initrd mounts cidata, and mount creates /run/mount, which outlives
# switch-root. A 0700 one denies every user manager its mount watch
# (docs/disk-encryption.md, "The initrd network").
check_run_mount() {
    local mode
    mode=$(stat -c %a /run/mount) || fail "cannot stat /run/mount"
    [ "${mode}" = 755 ] || fail "/run/mount is ${mode}, not 755"
    ok "/run/mount is 755"
}

# One property of a unit, from `systemctl show`.
unit_property() {
    systemctl show -p "$2" --value "$1" || fail "could not read ${2} of ${1}"
}

# lima-guestagent runs the binary Lima ships on its cidata disk, so it must run
# where that binary exists (a Lima box) and be skipped, not failed, where it
# does not (a libvirt box). /mnt/lima-cidata is root's 0700 mount. A unit
# that is missing or renamed is neither: systemctl show prints inactive for
# any name, so the unit must be loaded, and a skip must come from a condition
# systemd evaluated (ConditionTimestamp set).
check_guestagent() {
    local unit=lima-guestagent.service load state result evaluated
    load=$(unit_property "${unit}" LoadState)
    [ "${load}" = loaded ] || fail "${unit} is not loaded (LoadState=${load}): the unit is missing or renamed"
    state=$(unit_property "${unit}" ActiveState)
    if sudo -n test -e /mnt/lima-cidata/lima-guestagent; then
        [ "${state}" = active ] ||
            fail "Lima's cidata ships the guest agent, but ${unit} is ${state}"
        ok "${unit} is active, and Lima's cidata ships its binary"
        return
    fi
    result=$(unit_property "${unit}" ConditionResult)
    evaluated=$(unit_property "${unit}" ConditionTimestamp)
    [ -n "${evaluated}" ] ||
        fail "the cidata has no guest agent, but systemd never evaluated the condition of ${unit} (ConditionTimestamp is empty)"
    [ "${result}" = no ] ||
        fail "the cidata has no guest agent, but ${unit} has ConditionResult=${result}, not no"
    [ "${state}" != failed ] || fail "the cidata has no guest agent, and ${unit} failed"
    ok "${unit} is skipped: the cidata has no guest agent binary"
}

# The agent's first `git clone git@github.com:...` must not stop at a
# trust-on-first-use prompt: /etc/ssh/ssh_known_hosts pins github.com
# (nix/base.md). An empty user known_hosts leaves only that file to vouch.
check_github_host_keys() {
    local out status=0
    ssh-keygen -F github.com -f /etc/ssh/ssh_known_hosts >/dev/null ||
        fail "/etc/ssh/ssh_known_hosts has no github.com key"
    out=$(as_agent ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -T git@github.com 2>&1) || status=$?
    case ${out} in
    *"Host key verification failed"*)
        fail "the agent does not trust github.com's host key from /etc/ssh/ssh_known_hosts: ${out}" ;;
    *"successfully authenticated"* | *"Permission denied"*) ;;
    *) fail "ssh to github.com as the agent ended neither in auth nor a refusal (exit ${status}): ${out}" ;;
    esac
    ok "the agent trusts github.com's host key without a prompt"
}

# The in-box rebuild chokepoint exists and refuses a call without an action.
check_rebuild_helper() {
    local status=0 out
    out=$(yolobox-guest rebuild 2>&1) || status=$?
    [ "${status}" -eq 2 ] ||
        fail "yolobox-guest rebuild without an action exited ${status}, not 2: ${out}"
    ok "yolobox-guest rebuild exists and refuses a call without switch or boot"
}

# The one qdisc that stays: net.core.default_qdisc is fq_codel.
check_default_qdisc() {
    local qdisc
    qdisc=$(sysctl -n net.core.default_qdisc) || fail "could not read net.core.default_qdisc"
    [ "${qdisc}" = fq_codel ] || fail "net.core.default_qdisc is '${qdisc}', not fq_codel"
    as_agent unshare -Urn tc qdisc add dev lo root fq_codel ||
        fail "the agent could not attach fq_codel in its own namespace"
    ok "default qdisc is fq_codel and the agent can attach it"
}

check_podman() {
    # Clear a leftover from an interrupted run; -f makes a missing one fine.
    as_agent podman network rm -f "${HK_NET}" >/dev/null
    as_agent podman run --rm docker.io/library/busybox true ||
        fail "rootless podman run failed"
    ok "rootless podman runs a container"
    as_agent podman network create "${HK_NET}" >/dev/null ||
        fail "podman network create ${HK_NET} failed"
    NET_CREATED=1
    as_agent podman run --rm --network "${HK_NET}" docker.io/library/busybox true ||
        fail "podman run on a bridge network failed"
    as_agent podman network rm "${HK_NET}" >/dev/null || fail "podman network rm ${HK_NET} failed"
    NET_CREATED=""
    ok "rootless podman runs a container on a bridge network"
}

check_chromium() {
    local dom
    # Chromium logs a dbus error per second without a session bus; keep its
    # stderr for the failure message only.
    dom=$(as_agent timeout 120 chromium --headless=new --disable-gpu --dump-dom https://example.com 2>"${WORK}/chromium.err") ||
        fail "headless chromium failed: $(tail -n 5 "${WORK}/chromium.err")"
    grep -q Example <<<"${dom}" || fail "chromium dumped a DOM without 'Example'"
    ok "headless chromium renders https://example.com"
}

check_nix_build() {
    as_agent nix-build --no-out-link "${WORK}/build.nix" >/dev/null ||
        fail "nix-build of a fresh derivation failed as the agent"
    ok "agent can run a sandboxed nix build"
}

# strace -p on an agent-owned process. The target blocks on the fifo; strace
# must print its attach line, after which the fifo is written, the target
# exits and strace follows it out. No timer orders any of it: a refused attach
# (ptrace_scope, a seccomp or LSM rule) prints its error in the attach line's
# place.
check_strace() {
    local first rest
    SLEEPER_PID=$(as_agent "${WORK}/sleeper.sh" | tail -n 1)
    case ${SLEEPER_PID} in
    '' | *[!0-9]*) fail "could not start the strace target: '${SLEEPER_PID}'" ;;
    esac
    exec 3< <(as_agent strace -p "${SLEEPER_PID}" -e trace=none 2>&1)
    read -r -u 3 first || fail "strace -p ${SLEEPER_PID} printed nothing"
    case ${first} in
    *"Process ${SLEEPER_PID} attached"*) ;;
    *) fail "strace -p on an agent process did not attach: ${first}" ;;
    esac
    echo go >"${WORK}/fifo"
    rest=$(cat <&3)
    exec 3<&-
    SLEEPER_PID=""
    case ${rest} in
    *"+++ exited with 0 +++"*) ;;
    *) fail "strace -p attached, but did not follow the target to its exit: ${rest}" ;;
    esac
    ok "strace -p attaches to an agent-owned process and follows it to its exit"
}

# The first-boot install is a user oneshot (nix/harnesses.nix). `systemctl
# start` on a oneshot returns when its run ends, nonzero if it failed: it joins
# a run in progress, and it runs a unit that is waiting out Restart=on-failure
# at once. So the versions below are checked after the install, never during it.
# The network flag and the user manager are started first for the same reason:
# each start blocks until its unit is up, and is a no-op when it already is.
await_harness_install() {
    sudo -n systemctl start yolobox-network-ready.service "user@${AGENT_UID}.service" ||
        fail "the network flag or the agent's user manager did not start"
    sudo -n systemctl --user -M "${AGENT_USER}@" start yolobox-harness-install.service ||
        fail "yolobox-harness-install failed; see: journalctl --user -u yolobox-harness-install"
    ok "yolobox-harness-install finished"
}

check_vendors() {
    local tool
    await_harness_install
    for tool in claude pi; do
        as_agent "${tool}" --version >/dev/null || fail "${tool} --version failed as the agent"
        ok "${tool} --version"
    done
}

# user@.service has OOMScoreAdjust=-500 (nix/base.md, "Memory pressure"), which
# every process under the agent's user manager inherits unless its unit sets
# its own. The herdr server and the panes it forks are the heavy, restartable
# part of a session, so a -500 there would make them the last thing the kernel
# kills, behind the manager that holds them. nix/herd-server.nix sets 0.
check_herdr_server_oom_score() {
    local unit=herdr-server.service pid score
    pid=$(sudo -n systemctl --user -M "${AGENT_USER}@" show -p MainPID --value "${unit}") ||
        fail "could not read the main pid of the agent's ${unit}"
    [ "${pid}" -gt 0 ] || fail "the agent's ${unit} is not running"
    score=$(sudo -n cat "/proc/${pid}/oom_score_adj") ||
        fail "could not read oom_score_adj of ${unit} (pid ${pid})"
    [ "${score}" -ge 0 ] ||
        fail "${unit} runs with oom_score_adj ${score}, inherited from user@.service; it must be 0 or more"
    ok "${unit} runs with oom_score_adj ${score}"
}

# What hidepid must leave working for the agent: its user manager (the same
# uid, so unaffected), logind (which runs with wheel as a supplementary group,
# nix/hardening.nix, and tracks the lingering agent) and the system bus
# (dbus-broker reads a peer's credentials from the socket, not from /proc).
check_agent_session_services() {
    local state
    state=$(as_agent systemctl --user is-active herdr-server) ||
        fail "herdr-server is ${state:-unknown} under the agent's user manager"
    as_agent loginctl --no-pager user-status "${AGENT_USER}" >/dev/null ||
        fail "loginctl user-status ${AGENT_USER} failed as the agent: logind no longer tracks the agent"
    as_agent busctl --no-pager status >/dev/null ||
        fail "busctl status failed as the agent: the system bus does not answer it"
    ok "the agent's user manager, logind and the system bus answer the agent"
}

check_rosetta() {
    local hello
    [ "$(head -n 1 /proc/sys/fs/binfmt_misc/rosetta)" = enabled ] ||
        fail "binfmt_misc rosetta is not enabled"
    # An x86_64 build is substituted from the binary cache and run directly:
    # the only way it executes on this aarch64 box is through rosetta.
    hello=$(as_agent nix build --no-link --print-out-paths nixpkgs#legacyPackages.x86_64-linux.hello) ||
        fail "could not fetch x86_64 hello"
    as_agent "${hello}/bin/hello" >/dev/null || fail "x86_64 hello did not run under rosetta"
    ok "rosetta runs an x86_64 binary"
}

compat_checks() {
    check_net
    check_default_qdisc
    check_podman
    check_chromium
    check_nix_build
    check_strace
    check_vendors
    check_herdr_server_oom_score
    check_agent_session_services
    # Rosetta follows the guest's CPU, as nix/base.nix gates it: an aarch64
    # guest runs only under Lima/vz on Apple silicon and must have it.
    if [ "$(uname -m)" = aarch64 ]; then
        check_rosetta
    fi
    check_no_host_os_stamp
    check_etc_environment
    check_run_mount
    check_guestagent
    check_rebuild_helper
    check_github_host_keys
}

main() {
    [ "$(id -u)" -ne "${AGENT_UID}" ] || fail "run this as the operator, not the agent"
    sudo -n true || fail "passwordless sudo is required (run as the operator)"
    AGENT_HOME=$(getent passwd "${AGENT_USER}" | cut -d: -f6)
    [ -n "${AGENT_HOME}" ] || fail "no account ${AGENT_USER} in passwd"
    case ${ONLY:-both} in
    both | hardening | compat) ;;
    *) fail "ONLY must be hardening, compat or unset, got '${ONLY}'" ;;
    esac
    parse_extra_wrappers
    trap cleanup EXIT
    stage_helpers
    case ${ONLY:-both} in
    both)
        hardening_checks
        compat_checks
        ;;
    hardening) hardening_checks ;;
    compat) compat_checks ;;
    esac
    echo "box-hardening-check: all checks passed"
}

# stdin is /dev/null so no check can swallow the rest of this script when it
# is piped in through `bash -s`.
main "$@" </dev/null
