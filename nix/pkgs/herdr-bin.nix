{ stdenvNoCC, fetchurl, binutils, version, hash }:

# herdr is source-built in nixpkgs (rust + zig, two fixed-output hashes) so
# claude-code's overrideAttrs+fetchurl valve shape doesn't apply to it
# (plan critic B5) — this is the separate binary-release derivation instead,
# fetching the upstream linux-aarch64/linux-x86_64 asset straight off GitHub
# releases per the manifest herdr's own install.sh reads
# (https://herdr.dev/latest.json). `uname.processor` is nixpkgs' own name for
# the arch component of these asset names ("aarch64" / "x86_64",
# lib/systems/default.nix).
stdenvNoCC.mkDerivation {
  pname = "herdr";
  inherit version;

  src = fetchurl {
    url = "https://github.com/herdrdev/herdr/releases/download/v${version}/herdr-linux-${stdenvNoCC.hostPlatform.uname.processor}";
    inherit hash;
  };

  nativeBuildInputs = [ binutils ];

  dontUnpack = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 "$src" "$out/bin/herdr"
    # The design rests on this asset being static (no PT_INTERP, no
    # PT_DYNAMIC): that is what lets it run on NixOS unpatched, with none of
    # the nix-ld dance a dynamically linked upstream binary would otherwise
    # need. A dynamic asset would still install fine and only fail at unit
    # start with "No such file or directory", so catch it here instead.
    if readelf -l "$out/bin/herdr" | grep -q INTERP; then
      echo "herdr-bin: $out/bin/herdr carries PT_INTERP — no longer static, nix-ld wiring is needed" >&2
      exit 1
    fi
    runHook postInstall
  '';

  meta = {
    description = "herdr — host-side terminal multiplexer with AI-agent awareness";
    homepage = "https://herdr.dev";
    mainProgram = "herdr";
    platforms = [ "aarch64-linux" "x86_64-linux" ];
  };
}
