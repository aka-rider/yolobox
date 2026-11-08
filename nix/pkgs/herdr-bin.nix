{ stdenvNoCC, fetchurl }:

# The pinned herdr release, fetched as the upstream linux asset for the
# guest's CPU straight off GitHub releases (the manifest herdr's own
# install.sh reads is https://herdr.dev/latest.json). A saved SSH machine
# needs the guest server's endpoint_protocol_generation to equal the Mac
# client's exactly, so bump version and hashes together when the client
# moves to a new generation. Both assets are static (no PT_INTERP), so they
# run here unpatched.
let
  version = "0.9.5";
  hashes = {
    aarch64-linux = "sha256-F9dTby2lCFlSNoDvJ53/ysjkojX05G/o2mwuxEFBiXw=";
    x86_64-linux = "sha256-FNnovItKngEY13wyJm921GHwm0A0LrI3UpYL2J9Zkp0=";
  };
  system = stdenvNoCC.hostPlatform.system;
  # Upstream's asset name per Nix system; the keys are the supported platforms.
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
    hash = hashes.${system} or (throw "herdr ${version} has no pinned hash for ${system}, the system this guest is built for; add hashes.${system} = \"sha256-...\" in nix/pkgs/herdr-bin.nix");
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
