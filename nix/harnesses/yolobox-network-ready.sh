#!/usr/bin/env bash
# Drops the flag that the network works, but only when the NIC holds an IPv4
# address now and the DHCP lease brought a nameserver: a guest without one
# reaches no name, and the install the flag gates would fail on DNS. dhcpcd
# keeps its lease file across boots, so the lease alone can be last boot's.
# NETWORK_READY_FLAG and YOLOBOX_GUEST_NIC are set by the build. On failure the
# reason is printed on stderr and no flag is made.

export LC_ALL=C

if ! addresses="$(ip -4 -o addr show dev "$YOLOBOX_GUEST_NIC")"; then
    printf 'yolobox-network-ready: ip could not list the addresses of %s\n' "$YOLOBOX_GUEST_NIC" >&2
    exit 1
fi

# One address per line: index, name, family, address, ...
has_address=false
while IFS= read -r line; do
    read -r _ _ family _ <<<"$line"
    if [[ $family == inet ]]; then
        has_address=true
        break
    fi
done <<<"$addresses"
if [[ $has_address != true ]]; then
    printf 'yolobox-network-ready: %s has no IPv4 address yet\n' "$YOLOBOX_GUEST_NIC" >&2
    exit 1
fi

if ! lease="$(dhcpcd --dumplease "$YOLOBOX_GUEST_NIC")"; then
    printf 'yolobox-network-ready: dhcpcd could not dump the lease of %s\n' "$YOLOBOX_GUEST_NIC" >&2
    exit 1
fi

while IFS= read -r line; do
    if [[ $line =~ ^domain_name_servers=[^[:space:]].*$ ]]; then
        touch "$NETWORK_READY_FLAG"
        exit 0
    fi
done <<<"$lease"

printf 'yolobox-network-ready: the lease of %s brought no domain_name_servers; it said:\n%s\n' "$YOLOBOX_GUEST_NIC" "$lease" >&2
exit 1
