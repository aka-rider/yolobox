# shellcheck shell=bash
# Runs in the initrd, before the initrd sshd starts: copies lima's operator
# key out of the cidata ISO into the file sshd reads for root. See nix/luks.nix.
if [ "$#" -ne 1 ]; then
  echo "usage: yolobox-initrd-authorized-keys <output file>" >&2
  exit 2
fi
out=$1

umask 077
mnt=$(mktemp -d /tmp/yolobox-cidata.XXXXXX)
trap 'umount "$mnt" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true' EXIT

mount -t iso9660 -o ro /dev/disk/by-label/cidata "$mnt"

# The same program nixos-lima's lima-init uses to read the key out of cidata's
# user-data (3-argument match is gawk-only).
gawk '
  match($0, /^([[:space:]]*)ssh-authorized-keys:/, m) { ident="^" m[1] "[[:space:]]+-[[:space:]]+"; flag=1; next }
  flag && $0 !~ ident { flag=0; next }
  flag && $0 ~ ident { sub(ident, ""); gsub("\"", ""); print $0 }
' "$mnt/user-data" > "$out.tmp"
mv "$out.tmp" "$out"

if [ ! -s "$out" ]; then
  echo "yolobox: no ssh-authorized-keys in lima's cidata user-data" >&2
  exit 1
fi
