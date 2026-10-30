{ lib, tailscale, fetchFromGitHub }:
let
  version = "1.104.4";
in
# The guard makes this file delete itself: once nixpkgs ships the pinned
# version, the build fails and names the file to remove (nix/pkgs/tailscale.md).
assert lib.assertMsg (lib.versionOlder tailscale.version version)
  "nix/pkgs/tailscale.nix: nixpkgs ships tailscale ${tailscale.version}, not older than the pin ${version}. Delete the file and the package line in nix/tailscale.nix.";
tailscale.overrideAttrs {
  inherit version;
  src = fetchFromGitHub {
    owner = "tailscale";
    repo = "tailscale";
    tag = "v${version}";
    hash = "sha256-sJh3oXFarS1s+flMfm4ZAE+0ZSwp8F3Rib76+qBFVIw=";
  };
  vendorHash = "sha256-RvaouD88naVNOPJBVcqMaAn+ovjXI4g83ShCdRiR5/Q=";
}
