# far2l

The code is `nix/pkgs/far2l.nix`, an override of nixpkgs' `far2l` (FAR Manager's Linux port). It is on both accounts' `PATH` from `environment.systemPackages` in `nix/base.nix`.

## Build

Nixpkgs builds NetRocks with openssl, libssh, samba, libnfs and neon, but never passes `aws-sdk-cpp`. far2l looks for it with `find_package(AWSSDK QUIET COMPONENTS s3)`, so the stock package ships with no S3 and says so only in a cmake warning. The override adds `aws-sdk-cpp.override { apis = [ "s3" ]; }`, which is core and s3 rather than the SDK's hundreds of APIs.

Every NetRocks backend is gated the same quiet way. A nixpkgs bump that drops a dependency would ship a far2l missing that protocol with no error anywhere. So the override's `postInstall` checks that each broker exists under `lib/far2l/Plugins/NetRocks/plug/` (FILE, SHELL, FTP, SFTP, SMB, NFS, WebDAV, AWS) and that the FTP broker links OpenSSL. FTPS is not a broker of its own, only OpenSSL linked into `NetRocks-FTP`. Recognise the failure by `far2l: NetRocks-<proto> missing`, or the FTPS message, in a `nixos-rebuild` log. The fix is restoring the dependency, not dropping the check.

`withGUI = false` and `withTTYX = false`. `DISPLAY=:0` is set box-wide and points at an Xvfb nobody sees (see [Display](../display.md#display)). A wx GUI build would open its window there. TTYX would route the clipboard and X key-modifier detection through that same invisible X server and read the Xvfb's keyboard state, not the Mac's. Plain TTY mode leaves the clipboard to the terminal through OSC 52, which does reach the Mac.

The override changes far2l's derivation hash, so far2l never comes from cache.nixos.org. The box compiles it locally, about 1m40s, and again after every nixpkgs bump. The s3-only SDK is not a local build. Hydra caches `aws-sdk-cpp` with `apis = [ "s3" ]` too, which the first build here confirmed by fetching it instead of compiling it.

`far2l --tty --help` with no tty attached never prints help. It detaches, reparents to init and sits there forever. Plain `far2l --help` prints and exits, so use that as a smoke test.

## S3

Leave the NetRocks site's login and password both empty. far2l then uses the SDK's `DefaultAWSCredentialsProviderChain`, which reads `AWS_CONFIG_FILE` and `AWS_PROFILE` and runs `credential_process`. That is exactly the guest config the AWS broker renders (see [Guest config](../../docs/aws.md#guest-config)). So `AWS_PROFILE=<P> far2l` gets that profile's broker credentials, fixed for the life of that far2l process, the same per-process rule as every other AWS client here. far2l itself refuses a site with only one of login and password filled in. Set the site's Region field, or leave it empty to fall back to the SDK's default region resolution. The site's host doubles as a custom endpoint (MinIO and the like).
