{
  description = "yolobox project dev shell (override `inputs.yolobox.url` to pin a different yolobox checkout)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    yolobox.url = "git+file:///home/xiii/wrk/yolobox";
  };

  outputs = { self, nixpkgs, yolobox }:
    {
      devShells.aarch64-linux.default = yolobox.lib.shell [ "rust" "postgres" ];
    };
}
