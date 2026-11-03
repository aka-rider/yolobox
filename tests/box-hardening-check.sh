#!/usr/bin/env bash
# Acceptance check for the box-hardening change (nix/hardening.nix). Runs
# INSIDE a yolobox guest, as the operator (the account with passwordless
# sudo). The host driver tests/box-hardening-e2e.sh pipes it over `yo ssh`;
# a person inside the box can run it directly.
#
#   EXTRA_WRAPPERS="<wrapper>" tests/box-hardening-check.sh
#
# EXTRA_WRAPPERS: space-separated setuid/capability wrapper names a box adds
#   through yolobox.hardening.extraWrappers in its /etc/yolobox/local.nix.
# ONLY=hardening | ONLY=compat: run just one half. Unset runs both. The
#   compat half ("nothing the agent needs broke") is the one to iterate on
#   against a box that is not hardened yet.
#
# On a libvirt guest (host-os linux) it also checks that the agent cannot
# log in as the operator with the forwarded 1Password agent, and that the
# firewall rule behind that is loaded.
#
# It stops at the first broken property and names it. There is no python in
# the box, so socket probes use perl and JSON uses jq.
set -Eeuo pipefail
# No globbing: the unquoted word lists below (wrapper names) must stay literal.
set -f

# Wrappers a hardened box keeps (nix/hardening.nix keptWrappers). Anything else in
# /run/wrappers/bin is an unexpected setuid/capability binary.
readonly WRAPPER_ALLOWLIST="sudo sudoedit unix_chkpwd newuidmap newgidmap"
readonly AGENT_UID=1000
readonly HK_NET=hk-test
# The addresses of the box's own network (nix/egress.nix, libvirt/domain.xml):
# the gateway is the host's loopback, the guest's address is enp0s1's.
readonly GATEWAY_ADDR=192.168.5.2
readonly GUEST_ADDR=192.168.5.15

WORK=""
SLEEPER_PID=""
NET_CREATED=""
PROBE_OUT=""
HOST_OS=""

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

# The host OS running this box, from /etc/yolobox/host-os: "darwin" (Lima on a
# Mac) or "linux" (libvirt on a Linux PC). Never guessed from the CPU: an Intel
# Mac runs an x86_64 Lima guest. fail exits only the command substitution it
# runs in, so callers take the result with `HOST_OS=$(host_os) || exit 1`.
host_os() {
    local file=/etc/yolobox/host-os host
    [ -f "${file}" ] || fail "${file} is missing: the box does not say which host runs it"
    host=$(cat "${file}") || fail "could not read ${file}"
    case ${host} in
    darwin | linux) printf '%s\n' "${host}" ;;
    *) fail "${file} must say darwin or linux, got '${host}'" ;;
    esac
}

trap 'echo "FAIL: unexpected error at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

# Run a command as the agent, the way an AI session sees it.
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
    sudo -iu agent env "XDG_RUNTIME_DIR=/run/user/${AGENT_UID}" "$@"
}

cleanup() {
    local status=$?
    trap - ERR
    if [ -n "${SLEEPER_PID}" ]; then
        as_agent kill "${SLEEPER_PID}" 2>/dev/null || true # already gone is fine
    fi
    if [ -n "${NET_CREATED}" ]; then
        as_agent podman network rm -f "${HK_NET}" >/dev/null ||
            warn "cleanup: could not remove podman network ${HK_NET}"
    fi
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
    exit "${status}"
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

check_cmdline() {
    local cmdline param
    cmdline=$(cat /proc/cmdline)
    # The first three are boot.kernelParams in nix/hardening.nix (a consistency
    # test compares them); nohibernate comes from security.protectKernelImage.
    for param in slab_nomerge page_alloc.shuffle=1 debugfs=off nohibernate; do
        printf '%s\n' "${cmdline}" | tr ' ' '\n' | grep -qxF -- "${param}" ||
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

    # Starts a long sleep detached from this script and prints its pid.
    cat >"${WORK}/sleeper.sh" <<'EOF'
#!/usr/bin/env bash
sleep 60 </dev/null >/dev/null 2>&1 &
echo $!
EOF
    chmod 755 "${WORK}/sleeper.sh"

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
# status means sudo, the login shell or perl broke, which says nothing about
# the hardening, so it gets a different message.
agent_probe() {
    local property_failed=$1 status=0
    shift
    PROBE_OUT=$(as_agent perl "$@" 2>&1) || status=$?
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
    if printf '%s\n' "${mods}" | grep -q "^${module} "; then
        fail "${module} is loaded in the kernel"
    fi
    ok "${label} socket refused for the agent (${PROBE_OUT}), ${module} not loaded"
}

# The sysctl read-back proves the setting; this proves the agent really meets it.
check_io_uring() {
    agent_probe "io_uring_setup was not refused with EPERM for the agent" "${WORK}/iouring.pl"
    ok "io_uring_setup ${PROBE_OUT} for the agent"
}

check_modules() {
    local out
    # AF_BLUETOOTH=31, SOCK_STREAM=1, BTPROTO_RFCOMM=3
    check_socket_refused bluetooth bluetooth 31 1 3
    # AF_INET=2, SOCK_STREAM=1, IPPROTO_SCTP=132
    check_socket_refused sctp sctp 2 1 132
    out=$(modprobe -n -v bluetooth 2>&1) || fail "modprobe -n -v bluetooth failed: ${out}"
    printf '%s\n' "${out}" | grep -q 'install' && printf '%s\n' "${out}" | grep -q 'bin/false' ||
        fail "modprobe -n -v bluetooth does not hit the install rule (want install ... bin/false): ${out}"
    ok "explicit modprobe of bluetooth is routed to bin/false"
}

# The agent is root of its own user and network namespace and can attach
# traffic-control objects there, which autoloads the module behind each one.
# The net/sched modules are blocked (nix/hardening.nix), except sch_fq_codel.
check_net_sched() {
    local out status=0 loaded
    if out=$(as_agent unshare -Urn tc qdisc add dev lo root netem delay 1ms 2>&1); then
        fail "the agent attached a netem qdisc in its own namespace (sch_netem autoloads)"
    fi
    case ${out} in
    *"Specified qdisc kind is unknown"*) ;;
    *) fail "tc qdisc add netem failed, but not with the kernel refusing the qdisc kind: ${out}" ;;
    esac
    ok "the agent cannot attach a netem qdisc (${out})"
    out=$(as_agent unshare -Urn bash "${WORK}/netsched.sh" 2>&1) || status=$?
    case ${status} in
    0) ;;
    3) fail "the agent attached a u32 classifier in its own namespace (cls_u32 autoloads): ${out}" ;;
    4) fail "fq_codel did not attach in the agent's namespace, so the kept qdisc is broken: ${out}" ;;
    *) fail "netsched.sh exited ${status}, not with the kernel refusing the classifier: ${out}" ;;
    esac
    ok "the agent cannot attach a u32 classifier (${out})"
    loaded=$(lsmod | awk '$1 ~ /^(cls|act|sch)_/ && $1 != "sch_fq_codel" {print $1}')
    [ -z "${loaded}" ] || fail "net/sched modules are loaded: $(echo ${loaded})"
    out=$(modprobe -n -v cls_u32 2>&1) || fail "modprobe -n -v cls_u32 failed: ${out}"
    printf '%s\n' "${out}" | grep -q 'bin/false' ||
        fail "modprobe -n -v cls_u32 does not hit the install rule (want install ... bin/false): ${out}"
    ok "no cls_, act_ or sch_ module is loaded but sch_fq_codel, and cls_u32 is routed to bin/false"
}

check_wrappers() {
    local want have name out caps expected_caps
    have=$(ls /run/wrappers/bin | sort -u)
    want=$(printf '%s\n' ${WRAPPER_ALLOWLIST} ${EXTRA_WRAPPERS:-} | sort -u)
    [ "${have}" = "${want}" ] || fail "/run/wrappers/bin differs from the allowlist (< unexpected, > missing)
$(diff <(printf '%s\n' "${have}") <(printf '%s\n' "${want}") || true)"
    ok "wrapper set is exactly: $(echo ${want})"

    for name in sudo sudoedit unix_chkpwd; do
        [ -u "/run/wrappers/bin/${name}" ] || fail "${name} lost its setuid bit"
    done
    ok "sudo, sudoedit and unix_chkpwd are setuid"

    # Sorted capability names the wrapper carries; "" when it has none.
    # getcap prints "path cap_x=ep" or "path = cap_x+ep" by libcap version.
    for name in ${have}; do
        out=$(getcap "/run/wrappers/bin/${name}") || fail "getcap ${name} failed"
        # grep exits 1 on the empty output of a wrapper without capabilities.
        caps=$(printf '%s\n' "${out}" | grep -o 'cap_[a-z_]*' | sort | paste -sd, -) || caps=""
        case ${name} in
        # nixpkgs grants cap_setpcap beside the one the name suggests
        # (getcap: "newuidmap cap_setuid,cap_setpcap=ep").
        newuidmap) expected_caps=cap_setpcap,cap_setuid ;;
        newgidmap) expected_caps=cap_setgid,cap_setpcap ;;
        *) expected_caps="" ;;
        esac
        [ "${caps}" = "${expected_caps}" ] ||
            fail "wrapper ${name} has capabilities '${caps}', want '${expected_caps}'"
    done
    ok "only newuidmap (cap_setuid, cap_setpcap) and newgidmap (cap_setgid, cap_setpcap) carry capabilities"
}

check_no_su() {
    local found resolved
    # The store's plain `su` stays in /run/current-system/sw/bin; without a
    # setuid wrapper it cannot do anything. What must be gone is a su the
    # agent can run as root: no wrapper, and no setuid file behind the name.
    found=$(as_agent bash -c "command -v su || echo NOSU") ||
        fail "could not run bash as the agent (sudo failed): ${found}"
    if [ "${found}" = NOSU ]; then
        ok "su is not on the agent's PATH"
        return
    fi
    case ${found} in
    /run/wrappers/*) fail "the agent's su is the wrapper ${found}" ;;
    esac
    resolved=$(readlink -f "${found}")
    [ ! -u "${resolved}" ] || fail "the agent's su (${resolved}) is setuid"
    ok "the agent's su (${found}) has no setuid wrapper"
}

hardening_checks() {
    check_sysctls
    check_cmdline
    check_modules
    check_net_sched
    check_io_uring
    check_wrappers
    check_no_su
}

# ------------------------------------------------------------ nothing broke

check_net() {
    local out
    out=$(yolobox-guest net-check) || fail "yolobox-guest net-check exited nonzero: ${out}"
    printf '%s\n' "${out}" | grep -qx 'egress=ok' || fail "net-check: want egress=ok, got: ${out}"
    ok "net-check: egress=ok"
}

# The gateway is the host's own loopback, so a connection to its port 22 would
# reach the host's sshd. The egress table must refuse it for the agent.
check_gateway_ssh_closed() {
    agent_probe "the agent connected to ${GATEWAY_ADDR}:22, the host's loopback" \
        "${WORK}/tcp.pl" "${GATEWAY_ADDR}" 22
    ok "the agent cannot reach ${GATEWAY_ADDR}:22 (${PROBE_OUT})"
}

# A libvirt guest: the operator's key is served by the host's 1Password agent,
# which the agent account reaches through its forwarded socket. sshd must
# therefore refuse the operator from every address but the gateway's, and the
# firewall must stop the agent claiming that address over loopback.
# $1 is the operator's account name.
check_operator_login_refused() {
    local operator=$1 addr out
    out=$(as_agent printenv SSH_AUTH_SOCK) ||
        fail "the agent has no SSH_AUTH_SOCK, so a login probe would prove nothing"
    [ -n "${out}" ] || fail "the agent's SSH_AUTH_SOCK is empty"
    for addr in 127.0.0.1 "${GUEST_ADDR}"; do
        if out=$(as_agent ssh -o BatchMode=yes -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
            "${operator}@${addr}" true 2>&1); then
            fail "the agent logged in as ${operator} at ${addr} with its forwarded agent"
        fi
        case ${out} in
        *"Permission denied"*) ;;
        *) fail "ssh as ${operator} at ${addr} failed, but not with Permission denied: ${out}" ;;
        esac
        ok "the agent cannot log in as ${operator} at ${addr}"
    done
    # -C matches the rule by meaning; `iptables -S` prints its options in
    # another order than the one the unit adds them in.
    sudo -n iptables -C nixos-fw ! -i enp0s1 -s "${GATEWAY_ADDR}" -j DROP ||
        fail "nixos-fw has no rule dropping ${GATEWAY_ADDR} off enp0s1: $(sudo -n iptables -S nixos-fw)"
    ok "nixos-fw drops packets claiming ${GATEWAY_ADDR} on any interface but enp0s1"
}

# True on a libvirt guest, the Linux host's way of running the box.
is_libvirt_guest() {
    [ "${HOST_OS}" = linux ]
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
    printf '%s\n' "${dom}" | grep -q Example || fail "chromium dumped a DOM without 'Example'"
    ok "headless chromium renders https://example.com"
}

check_nix_build() {
    as_agent nix-build --no-out-link "${WORK}/build.nix" >/dev/null ||
        fail "nix-build of a fresh derivation failed as the agent"
    ok "agent can run a sandboxed nix build"
}

check_strace() {
    local status=0 out
    SLEEPER_PID=$(as_agent "${WORK}/sleeper.sh" | tail -n 1)
    case ${SLEEPER_PID} in
    '' | *[!0-9]*) fail "could not start the strace target: '${SLEEPER_PID}'" ;;
    esac
    # timeout exits 124 when strace was still attached after 2s: success.
    # A refused attach (ptrace_scope, a seccomp or LSM rule) exits 1 at once.
    out=$(as_agent timeout 2 strace -p "${SLEEPER_PID}" -e trace=none 2>&1) || status=$?
    [ "${status}" -eq 124 ] || fail "strace -p on an agent process did not stay attached (exit ${status}): ${out}"
    ok "strace -p attaches to an agent-owned process"
    as_agent kill "${SLEEPER_PID}" || fail "could not kill the strace target ${SLEEPER_PID}"
    SLEEPER_PID=""
}

# The first-boot install is a user oneshot (nix/harnesses.nix). `systemctl
# start` on a oneshot returns when its run ends, nonzero if it failed: it joins
# a run in progress, and it runs a unit that is waiting out Restart=on-failure
# at once. So the versions below are checked after the install, never during it.
# The network flag and the user manager are started first for the same reason:
# each start blocks until its unit is up, and is a no-op when it already is.
await_harness_install() {
    sudo systemctl start yolobox-network-ready.service "user@${AGENT_UID}.service" ||
        fail "the network flag or the agent's user manager did not start"
    sudo systemctl --user -M "agent@" start yolobox-harness-install.service ||
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
    check_gateway_ssh_closed
    check_default_qdisc
    check_podman
    check_chromium
    check_nix_build
    check_strace
    check_vendors
    # Rosetta exists only on Apple silicon: a darwin guest on aarch64 is a
    # Lima/vz box and must have it. An Intel Mac's x86_64 guest and a libvirt
    # guest have none.
    if [ "${HOST_OS}" = darwin ] && [ "$(uname -m)" = aarch64 ]; then
        check_rosetta
    fi
    if is_libvirt_guest; then
        check_operator_login_refused "$(id -un)"
    fi
}

main() {
    [ "$(id -u)" -ne "${AGENT_UID}" ] || fail "run this as the operator, not the agent"
    sudo -n true || fail "passwordless sudo is required (run as the operator)"
    HOST_OS=$(host_os) || exit 1
    trap cleanup EXIT
    stage_helpers
    case ${ONLY:-both} in
    both)
        hardening_checks
        compat_checks
        ;;
    hardening) hardening_checks ;;
    compat) compat_checks ;;
    *) fail "ONLY must be hardening, compat or unset, got '${ONLY}'" ;;
    esac
    echo "box-hardening-check: all checks passed"
}

# stdin is /dev/null so no check can swallow the rest of this script when it
# is piped in through `bash -s`.
main "$@" </dev/null
