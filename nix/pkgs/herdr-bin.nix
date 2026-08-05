{ stdenvNoCC, fetchurl, version, hash }:

# herdr is source-built in nixpkgs (rust + zig, two fixed-output hashes) so
# claude-code's overrideAttrs+fetchurl valve shape doesn't apply to it
# (plan critic B5) — this is the separate binary-release derivation instead,
# fetching the upstream linux-aarch64 asset straight off GitHub releases per
# the manifest herdr's own install.sh reads (https://herdr.dev/latest.json).
stdenvNoCC.mkDerivation {
  pname = "herdr";
  inherit version;

  src = fetchurl {
    url = "https://github.com/herdrdev/herdr/releases/download/v${version}/herdr-linux-aarch64";
    inherit hash;
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
    platforms = [ "aarch64-linux" ];
  };
}
