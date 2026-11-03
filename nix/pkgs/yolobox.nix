{ lib
, stdenvNoCC
, makeWrapper
, src
, version
, fzf
, git
, openssh
, curl
, python3
, coreutils
, lima
, xorriso
}:

let
  # A host ships only its own driver and the files that driver reads: macOS
  # runs Lima, Linux runs libvirt behind the host table. cli.driver_for imports
  # the driver module of the host, so the other one is never loaded.
  darwinFiles = ''
    rm "$out/libexec/yolobox/yolobox_cli/libvirt.py"
    rm "$out/libexec/yolobox/yolobox_cli/libvirt_host.py"
    install -Dm644 lima/yolobox.yaml "$out/libexec/yolobox/lima/yolobox.yaml"
  '';
  linuxFiles = ''
    rm "$out/libexec/yolobox/yolobox_cli/lima.py"
    install -Dm644 libvirt/domain.xml "$out/libexec/yolobox/libvirt/domain.xml"
    install -Dm644 linux/yolobox-host.nft "$out/libexec/yolobox/linux/yolobox-host.nft"
    install -Dm644 linux/yolobox-host.service "$out/libexec/yolobox/linux/yolobox-host.service"
    install -Dm644 linux/host-setup.sh "$out/libexec/yolobox/linux/host-setup.sh"
    install -Dm644 linux/yolobox-libvirt-hook.sh "$out/libexec/yolobox/linux/yolobox-libvirt-hook.sh"
  '';
in
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
    cp -r yolobox_cli "$out/libexec/yolobox/yolobox_cli"
    ${if stdenvNoCC.hostPlatform.isDarwin then darwinFiles else linuxFiles}
    substituteInPlace "$out/libexec/yolobox/yolobox_cli/config.py" \
      --replace-fail 'YO_VERSION = "dev"' 'YO_VERSION = "${version}"'

    patchShebangs "$out/libexec/yolobox/yo" "$out/libexec/yolobox/aws-broker"

    makeWrapper "$out/libexec/yolobox/yo" "$out/bin/yo" \
      --prefix PATH : ${lib.makeBinPath ([ fzf git openssh curl python3 coreutils ]
        ++ lib.optional stdenvNoCC.hostPlatform.isDarwin lima
        ++ lib.optional stdenvNoCC.hostPlatform.isLinux xorriso)}

    runHook postInstall
  '';

  meta = {
    description = "yolobox — NixOS VM devbox for AI agents, run by Lima on a Mac";
    homepage = "https://github.com/aka-rider/yolobox";
    license = lib.licenses.mit;
    mainProgram = "yo";
    platforms = [ "aarch64-darwin" "aarch64-linux" "x86_64-linux" ];
  };
}
