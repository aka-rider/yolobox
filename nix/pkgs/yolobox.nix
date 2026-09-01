{ lib
, stdenvNoCC
, makeWrapper
, src
, version
, fzf
, jq
, git
, openssh
, curl
, python3
, coreutils
, gnused
, gawk
, lima
}:

stdenvNoCC.mkDerivation {
  pname = "yolobox";
  inherit version src;

  nativeBuildInputs = [ makeWrapper ];

  dontBuild = true;
  dontConfigure = true;

  installPhase = ''
    runHook preInstall

    install -Dm755 yo "$out/libexec/yolobox/yo"
    install -Dm755 aws-broker "$out/libexec/yolobox/aws-broker"
    install -Dm644 lima/yolobox.yaml "$out/libexec/yolobox/lima/yolobox.yaml"

    substituteInPlace "$out/libexec/yolobox/yo" \
      --replace-fail 'YO_VERSION="dev"' 'YO_VERSION="${version}"'

    patchShebangs "$out/libexec/yolobox/yo" "$out/libexec/yolobox/aws-broker"

    makeWrapper "$out/libexec/yolobox/yo" "$out/bin/yo" \
      --prefix PATH : ${lib.makeBinPath ([ fzf jq git openssh curl python3 coreutils gnused gawk ]
        ++ lib.optional stdenvNoCC.hostPlatform.isDarwin lima)}

    runHook postInstall
  '';

  meta = {
    description = "yolobox — NixOS VM devbox for AI agents, run by Lima on a Mac";
    homepage = "https://github.com/aka-rider/yolobox";
    license = lib.licenses.mit;
    mainProgram = "yo";
    platforms = [ "aarch64-darwin" "aarch64-linux" ];
  };
}
