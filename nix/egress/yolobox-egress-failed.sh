# shellcheck shell=bash
# OnFailure of yolobox-egress.service: say, at a level nobody misses, which
# table the box is running under. The unit failing is the ordinary way a bad
# local.nix shows up, and the one state that must not pass quietly is no table
# at all: nothing then stops the guest reaching the host. See nix/egress.md.
# The table name is the same as in nix/egress.nix; the chain names tell the
# tiers apart (output, output_recovery, output_lastresort).
table_family=inet
table_name=yolobox-egress

if listing=$(nft list table "$table_family" "$table_name" 2>&1); then
  case "$listing" in
    *"chain output_lastresort {"*)
      echo "<2>yolobox-egress: the egress rules failed to load; the last-resort table is in force: DHCP and established and related flows only, the box has no other network" >&2
      ;;
    *"chain output_recovery {"*)
      echo "<2>yolobox-egress: the egress rules failed to load; the recovery table is in force: no network except DHCP and replies" >&2
      ;;
    *)
      echo "<2>yolobox-egress: the unit failed, but a table named ${table_family} ${table_name} with neither fallback chain is loaded; check it with: nft list table ${table_family} ${table_name}" >&2
      ;;
  esac
else
  echo "<2>yolobox-egress: the egress rules failed to load and NO TABLE IS LOADED, so the guest can reach the host and the LAN. nft said: ${listing}" >&2
fi
echo "<2>yolobox-egress: roll back with: sudo nixos-rebuild switch --rollback" >&2
