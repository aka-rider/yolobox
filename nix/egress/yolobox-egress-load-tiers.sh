# shellcheck shell=bash
# ExecStartPre of yolobox-egress.service: put the best fallback table in place
# before the full rules are tried. The recovery table first; when nft cannot
# load even that (it shares statements with the full rules), the last-resort
# table, which uses almost none (not `ct direction`, which the recovery file
# needs). Both files name the table the full rules replace, so whichever loaded
# here stays until ExecStart swaps it. If neither loads the script fails, the
# unit fails, and yolobox-egress-failed.service says so. See nix/egress.md.
if [ "$#" -ne 2 ]; then
  echo "usage: yolobox-egress-load-tiers <recovery file> <last-resort file>" >&2
  exit 2
fi
recovery=$1
lastresort=$2

# `if` and not `||`: the failure of the recovery load is the branch, and nft has
# already printed why on stderr.
if nft --file "$recovery"; then
  exit 0
fi
# <3> is the journal's "error" level, which systemd reads off the line.
echo "<3>yolobox-egress: the recovery table failed to load; loading the last-resort table" >&2
nft --file "$lastresort"
echo "<3>yolobox-egress: the last-resort table is in force: DHCP and established and related flows only, no other network" >&2
