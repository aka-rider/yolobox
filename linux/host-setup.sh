#!/usr/bin/env bash
# One-time, idempotent setup of a Fedora host for yolobox's libvirt path.
# Run as: sudo bash linux/host-setup.sh
# Each step says why it exists in the comment above it.
set -Eeuo pipefail
# The script greps virsh's output for English words (Autostart, Active, State),
# which a translated virsh would not print.
export LC_ALL=C

MIN_LIBVIRT="12.2.0"
VM_USER="yolobox-vm"
CONF_DIR="/etc/yolobox"
NFT_DEST="$CONF_DIR/yolobox-host.nft"
UNIT_DEST="/etc/systemd/system/yolobox-host.service"
POLKIT_RULE="/etc/polkit-1/rules.d/50-yolobox-libvirt.rules"
PASST_TMPFILES="/etc/tmpfiles.d/yolobox-passt.conf"
PASST_DIR="/run/libvirt/qemu/passt"
# libvirt's own account, which creates that directory; the hook refuses a
# directory owned by anyone else.
PASST_OWNER="qemu"
PASST_HOOK="/etc/libvirt/hooks/qemu.d/50-yolobox-passt"
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

# True when $2 is not yet a copy of $1 with mode $3, so a rerun installs
# nothing and restarts nothing.
needs_install() {
  ! cmp -s "$1" "$2" || [[ $(stat -c %a "$2") != "${3#0}" ]]
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
[[ -f $HERE/yolobox-libvirt-hook.sh ]] || die "missing $HERE/yolobox-libvirt-hook.sh"

# Checked before anything is installed or enabled, so a host that fails it is
# left as it was.
step "host resolver"
resolver="$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf)"
[[ $resolver =~ ^127\.[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "the first nameserver in /etc/resolv.conf is '${resolver:-none}', not a loopback address. passt would hand the guest that resolver, which sits on the LAN. Point the host at a local resolver (systemd-resolved's stub, 127.0.0.53, is one) and run this again"

step "libvirt repository and packages"
dnf copr enable -y @virtmaint-sig/virt-preview
dnf install -y libvirt-daemon-kvm libvirt-client edk2-ovmf passt passt-selinux xorriso nftables acl

# The system connection needs its daemons running, so this comes before the
# version gate and the network step. virtnetworkd is started only so the
# `default` network can be switched off.
step "libvirt daemons"
# The host table is tied to virtqemud (RequiredBy=, set up below), and the passt hook
# is read by it. The monolithic libvirtd would start domains past both, so a
# host that still runs or enables it is refused, not silently switched over
# under its running domains.
for unit in libvirtd.service libvirtd.socket; do
  if systemctl is-enabled --quiet "$unit" || systemctl is-active --quiet "$unit"; then
    die "the monolithic $unit is enabled or active; yolobox gates its host table and passt hook on virtqemud, which libvirtd would bypass. Switch the host to the modular daemons first (stop every domain, then: systemctl disable --now libvirtd.service libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket) and run this again"
  fi
done
systemctl enable --now virtqemud.socket virtstoraged.socket virtnetworkd.socket

# The daemon's version counts, not the client's: the daemon starts passt. A daemon
# that dnf upgraded but did not restart still reports the old one.
step "libvirt version gate"
daemon_report="$("${VIRSH[@]}" version --daemon)"
libvirt_version="$(sed -n 's/^Running against daemon:[[:space:]]*//Ip' <<<"$daemon_report" | head -n1 | tr -d '[:space:]')"
[[ $libvirt_version =~ ^[0-9]+(\.[0-9]+)*$ ]] ||
  die "cannot read the libvirt daemon's version from 'virsh version --daemon': $daemon_report"
version_at_least "$libvirt_version" "$MIN_LIBVIRT" ||
  die "the libvirt daemon is $libvirt_version, older than $MIN_LIBVIRT; passt needs the interface route option. Check that the virt-preview repository is enabled and its libvirt installed, then restart the daemon (systemctl restart virtqemud) and run this again"

step "libvirt's default network off"
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

step "polkit rule for $SUDO_USER"
install -d -m0755 "$(dirname "$POLKIT_RULE")"
cat >"$POLKIT_RULE" <<RULE
polkit.addRule(function(action, subject) {
  if (action.id == "org.libvirt.unix.manage" && subject.user == "$SUDO_USER") {
    return polkit.Result.YES;
  }
});
RULE
chmod 0644 "$POLKIT_RULE"

step "system account $VM_USER"
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
# virtqemud starts.
step "passt socket directory"
# A symlink there would send the ACL, and the hook's, to whatever it points at.
[[ ! -L $PASST_DIR ]] || die "$PASST_DIR is a symlink, which libvirt never makes; remove it and run this again"
cat >"$PASST_TMPFILES" <<RULE
d $PASST_DIR 0755 $PASST_OWNER $PASST_OWNER -
a+ $PASST_DIR - - - - u:$VM_USER:rwx,m::rwx
RULE
chmod 0644 "$PASST_TMPFILES"
systemd-tmpfiles --create "$PASST_TMPFILES"
setfacl -P -m "u:$VM_USER:rwx,m::rwx" "$PASST_DIR"

# libvirt recomputes that directory's ACL mask whenever it starts another passt
# domain, which cuts yolobox-vm's entry down to r-x. A qemu hook re-applies the
# same fixed setfacl before a yolobox domain starts and after any domain starts
# or is released. virtqemud reads its hook directory only when it starts, so it
# is restarted if it runs and has been running since before the hook file was
# written; running domains are not touched. That is read from the two
# timestamps, not from "this run installed the hook", so a run that died
# between the install and the restart is finished by the next one.
step "libvirt hook that keeps the passt ACL"
if needs_install "$HERE/yolobox-libvirt-hook.sh" "$PASST_HOOK" 0755; then
  install -D -m0755 "$HERE/yolobox-libvirt-hook.sh" "$PASST_HOOK"
fi
restorecon -F "$PASST_HOOK"
# An empty start time means virtqemud has not been active since boot.
qemud_started="$(systemctl show --timestamp=unix --property=ActiveEnterTimestamp --value virtqemud.service)"
if [[ $qemud_started =~ ^@([0-9]+)$ ]]; then
  if ((BASH_REMATCH[1] <= $(stat -c %Y "$PASST_HOOK"))); then
    systemctl try-restart virtqemud.service
  fi
elif [[ -n $qemud_started ]]; then
  die "cannot read virtqemud's start time: 'systemctl show --timestamp=unix' printed '$qemud_started', not @<seconds> (systemd 247 or newer prints that). Restart virtqemud by hand (systemctl try-restart virtqemud.service) so it loads $PASST_HOOK"
fi

step "host egress table"
[[ -x /usr/sbin/nft ]] || die "/usr/sbin/nft is missing after installing nftables"
/usr/sbin/nft -c -f "$HERE/yolobox-host.nft"
if needs_install "$HERE/yolobox-host.nft" "$NFT_DEST" 0644; then
  install -D -m0644 "$HERE/yolobox-host.nft" "$NFT_DEST"
fi
if needs_install "$HERE/yolobox-host.service" "$UNIT_DEST" 0644; then
  install -D -m0644 "$HERE/yolobox-host.service" "$UNIT_DEST"
fi
systemctl daemon-reload
# enable --now does nothing for a unit that is already active, so an active
# unit is reloaded on every run, whether or not the file changed: the loaded
# table may differ from the file (deleted or edited by hand, or a run that died
# after the install and before the reload). A reload is one atomic nft -f and
# idempotent. Not a restart: the unit is RequiredBy=virtqemud, and a restart
# would restart virtqemud with it, which a reload does not.
if systemctl is-active --quiet yolobox-host.service; then
  systemctl enable yolobox-host.service
  systemctl reload yolobox-host.service
else
  systemctl enable --now yolobox-host.service
fi

step "storage pool $POOL_NAME"
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
step "operator key"
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
  passt socket directory $PASST_DIR writable by $VM_USER, kept so by $PASST_HOOK
  account $VM_USER, table 'inet yolobox-host' via yolobox-host.service
  resolver $resolver, pool $POOL_NAME at $POOL_PATH
SUMMARY
if [[ -n $operator_reminder ]]; then
  printf '\n%s\n' "$operator_reminder"
fi
