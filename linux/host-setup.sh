#!/usr/bin/env bash
# One-time, idempotent setup of a Fedora host for yolobox's libvirt path.
# Run as: sudo bash linux/host-setup.sh
# The reasons for each step, and how to recognise a failure, are in
# linux/host-setup.md.
set -Eeuo pipefail

MIN_LIBVIRT="12.2.0"
VM_USER="yolobox-vm"
CONF_DIR="/etc/yolobox"
NFT_DEST="$CONF_DIR/yolobox-host.nft"
UNIT_DEST="/etc/systemd/system/yolobox-host.service"
POLKIT_RULE="/etc/polkit-1/rules.d/50-yolobox-libvirt.rules"
PASST_TMPFILES="/etc/tmpfiles.d/yolobox-passt.conf"
PASST_DIR="/run/libvirt/qemu/passt"
POOL_NAME="default"
POOL_PATH="/var/lib/libvirt/images"
VIRSH=(virsh -c qemu:///system)

STEP="start"
step() {
  STEP="$1"
  printf '==> %s\n' "$1"
}
trap 'printf "host-setup: failed during: %s\n" "$STEP" >&2' ERR

die() {
  printf 'host-setup: %s\n' "$1" >&2
  exit 1
}

# True when dotted version $1 is at least $2. Numeric per field, so 12.10.0
# is newer than 12.2.0.
version_at_least() {
  local -a have want
  local i
  IFS=. read -r -a have <<<"$1"
  IFS=. read -r -a want <<<"$2"
  for i in 0 1 2; do
    if ((${have[i]:-0} > ${want[i]:-0})); then return 0; fi
    if ((${have[i]:-0} < ${want[i]:-0})); then return 1; fi
  done
  return 0
}

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]] || die "run through sudo from the operator's own account (SUDO_USER is unset or root)"
[[ $SUDO_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "SUDO_USER is not a plain account name: $SUDO_USER"
id -u "$SUDO_USER" >/dev/null || die "no such account: $SUDO_USER"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f $HERE/yolobox-host.nft ]] || die "missing $HERE/yolobox-host.nft"
[[ -f $HERE/yolobox-host.service ]] || die "missing $HERE/yolobox-host.service"

# Checked before anything is installed or enabled, so a host that fails it is
# left as it was.
step "(h) host resolver"
resolver="$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf)"
[[ $resolver =~ ^127\.[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "the first nameserver in /etc/resolv.conf is '${resolver:-none}', not a loopback address. passt would hand the guest that resolver, which sits on the LAN. Point the host at a local resolver (systemd-resolved's stub, 127.0.0.53, is one) and run this again"

step "(a) libvirt repository and packages"
dnf copr enable -y @virtmaint-sig/virt-preview
dnf install -y libvirt-daemon-kvm libvirt-client edk2-ovmf passt passt-selinux xorriso nftables acl

# The system connection needs its daemons running, so this comes before the
# version gate and the network step. virtnetworkd is started only so the
# `default` network can be switched off.
step "(d) libvirt daemons"
systemctl enable --now virtqemud.socket virtstoraged.socket virtnetworkd.socket

# The daemon's version counts, not the client's: the daemon starts passt. A daemon
# that dnf upgraded but did not restart still reports the old one.
step "(b) libvirt version gate"
daemon_report="$("${VIRSH[@]}" version --daemon)"
libvirt_version="$(sed -n 's/^Running against daemon:[[:space:]]*//Ip' <<<"$daemon_report" | head -n1 | tr -d '[:space:]')"
[[ $libvirt_version =~ ^[0-9]+(\.[0-9]+)*$ ]] ||
  die "cannot read the libvirt daemon's version from 'virsh version --daemon': $daemon_report"
version_at_least "$libvirt_version" "$MIN_LIBVIRT" ||
  die "the libvirt daemon is $libvirt_version, older than $MIN_LIBVIRT; passt needs the interface route option. Check that the virt-preview repository is enabled and its libvirt installed, then restart the daemon (systemctl restart virtqemud) and run this again"

step "(c) libvirt's default network off"
networks="$("${VIRSH[@]}" net-list --all --name)"
if grep -Fxq -- default <<<"$networks"; then
  net_info="$("${VIRSH[@]}" net-info default)"
  if grep -Eq '^Autostart:[[:space:]]+yes' <<<"$net_info"; then
    "${VIRSH[@]}" net-autostart --disable default
    printf 'libvirt network default: autostart disabled\n'
  fi
  if grep -Eq '^Active:[[:space:]]+yes' <<<"$net_info"; then
    "${VIRSH[@]}" net-destroy default
    printf 'libvirt network default: stopped\n'
  fi
else
  printf 'libvirt network default: not defined\n'
fi

step "(e) polkit rule for $SUDO_USER"
install -d -m0755 "$(dirname "$POLKIT_RULE")"
cat >"$POLKIT_RULE" <<RULE
polkit.addRule(function(action, subject) {
  if (action.id == "org.libvirt.unix.manage" && subject.user == "$SUDO_USER") {
    return polkit.Result.YES;
  }
});
RULE
chmod 0644 "$POLKIT_RULE"

step "(f) system account $VM_USER"
if ! getent group "$VM_USER" >/dev/null; then
  groupadd --system "$VM_USER"
fi
if ! getent passwd "$VM_USER" >/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin -g "$VM_USER" "$VM_USER"
fi

# libvirt runs passt under the domain's DAC label, which is yolobox-vm, but it
# makes the socket directory owned by qemu with mode 0755, so passt's bind fails
# with "Permission denied". An ACL entry for yolobox-vm leaves the owner alone,
# and the mask is spelled out because an entry is only as wide as the mask.
# /run is a tmpfs, so a tmpfiles rule re-applies it on every boot, before
# virtqemud starts. See linux/host-setup.md.
step "(j) passt socket directory"
cat >"$PASST_TMPFILES" <<RULE
d $PASST_DIR 0755 qemu qemu -
a+ $PASST_DIR - - - - u:$VM_USER:rwx,m::rwx
RULE
chmod 0644 "$PASST_TMPFILES"
systemd-tmpfiles --create "$PASST_TMPFILES"
setfacl -m "u:$VM_USER:rwx,m::rwx" "$PASST_DIR"

step "(g) host egress table"
[[ -x /usr/sbin/nft ]] || die "/usr/sbin/nft is missing after installing nftables"
/usr/sbin/nft -c -f "$HERE/yolobox-host.nft"
install -D -m0644 "$HERE/yolobox-host.nft" "$NFT_DEST"
install -D -m0644 "$HERE/yolobox-host.service" "$UNIT_DEST"
systemctl daemon-reload
systemctl enable --now yolobox-host.service
# enable --now does nothing for a unit that is already active, so a changed
# file needs the restart.
systemctl restart yolobox-host.service

step "(i) storage pool $POOL_NAME"
pools="$("${VIRSH[@]}" pool-list --all --name)"
if ! grep -Fxq -- "$POOL_NAME" <<<"$pools"; then
  "${VIRSH[@]}" pool-define-as "$POOL_NAME" dir --target "$POOL_PATH"
  "${VIRSH[@]}" pool-build "$POOL_NAME"
fi
pool_info="$("${VIRSH[@]}" pool-info "$POOL_NAME")"
if ! grep -Eq '^State:[[:space:]]+running' <<<"$pool_info"; then
  "${VIRSH[@]}" pool-start "$POOL_NAME"
fi
if ! grep -Eq '^Autostart:[[:space:]]+yes' <<<"$pool_info"; then
  "${VIRSH[@]}" pool-autostart "$POOL_NAME"
fi

# The operator's key lives in the operator's 1Password agent, not in root's
# reach, so ask the agent as the operator. Only the comment is compared.
step "(k) operator key"
operator_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
[[ -n $operator_home ]] || die "no home directory for $SUDO_USER"
operator_reminder=""
if agent_keys="$(sudo -u "$SUDO_USER" env SSH_AUTH_SOCK="$operator_home/.1password/agent.sock" ssh-add -L 2>&1)"; then
  if ! awk 'NF == 3 && $3 == "yolobox-operator" { found = 1 } END { exit !found }' <<<"$agent_keys"; then
    operator_reminder="Still yours to do: create a 1Password SSH Key item (Ed25519) titled exactly
'yolobox-operator' in a vault your 1Password agent serves."
  fi
else
  operator_reminder="Could not check for the 'yolobox-operator' key: $operator_home/.1password/agent.sock
is missing or the agent is locked (ssh-add said: ${agent_keys:-nothing}). Make sure a 1Password SSH Key
item (Ed25519) titled exactly 'yolobox-operator' exists in a vault your agent serves."
fi

STEP="summary"
cat <<SUMMARY

host-setup done:
  libvirt daemon $libvirt_version, network 'default' off, daemons enabled
  polkit: $SUDO_USER may manage the system libvirt connection
  passt socket directory $PASST_DIR writable by $VM_USER
  account $VM_USER, table 'inet yolobox-host' via yolobox-host.service
  resolver $resolver, pool $POOL_NAME at $POOL_PATH
SUMMARY
if [[ -n $operator_reminder ]]; then
  printf '\n%s\n' "$operator_reminder"
fi
