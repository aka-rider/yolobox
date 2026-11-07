{ stdenvNoCC, fetchurl, version, hashes }:

# herdr is source-built in nixpkgs (rust + zig, two fixed-output hashes) so
# claude-code's overrideAttrs+fetchurl valve shape doesn't apply to it.
# This is the separate binary-release derivation instead, fetching the
# upstream linux asset for the guest's CPU straight off GitHub releases per
# the manifest herdr's own install.sh reads (https://herdr.dev/latest.json).
let
  system = stdenvNoCC.hostPlatform.system;
  # Upstream's asset name per Nix system; `hashes` carries one entry per
  # key here. The keys are also the supported-platform list.
  assets = {
    aarch64-linux = "herdr-linux-aarch64";
    x86_64-linux = "herdr-linux-x86_64";
  };
in
stdenvNoCC.mkDerivation {
  pname = "herdr";
  inherit version;

  # A box on a CPU the pin has no hash for must say which hash to add, not
  # fail with nix's bare "attribute missing".
  src = fetchurl {
    url = "https://github.com/herdrdev/herdr/releases/download/v${version}/${assets.${system}}";
    hash = hashes.${system} or (throw "yolobox.harness.herdr.hashes has no entry for ${system}, the system this guest is built for; add hashes.${system} = \"sha256-...\" for herdr ${version}");
  };

  dontUnpack = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 "$src" "$out/bin/herdr"
    runHook postInstall
  '';

  meta = {
    description = "herdr — host-side terminal multiplexer with AI-agent awareness";
    homepage = "https://herdr.dev";
    mainProgram = "herdr";
    platforms = builtins.attrNames assets;
  };
}
