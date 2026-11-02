# The two env vars a dynamically linked glibc binary needs to run under
# nix-ld (programs.nix-ld.enable, nix/base.nix). Shared so a systemd unit
# that execs such a binary with a spelled-out `environment` (rather than an
# interactive shell, which gets these for free) does not copy the paths a
# second time.
{
  NIX_LD = "/run/current-system/sw/share/nix-ld/lib/ld.so";
  NIX_LD_LIBRARY_PATH = "/run/current-system/sw/share/nix-ld/lib";
}
