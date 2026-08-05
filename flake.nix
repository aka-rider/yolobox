{
  description = "yolobox: NixOS VM devbox for AI agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-lima = {
      url = "github:nixos-lima/nixos-lima";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-lima }:
    let
      pkgs = import nixpkgs { system = "aarch64-linux"; };
      shellLib = import ./nix/shells { inherit pkgs; };
    in
    {
      nixosConfigurations.yolobox = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [
          nixos-lima.nixosModules.lima
          ./nix/base.nix
          ./nix/podman.nix
          ./nix/agentic.nix
        ];
      };

      devShells.aarch64-linux = shellLib.devShells;
      lib.shell = shellLib.shell;

      templates.default = {
        path = ./templates/default;
        description = "yolobox project: direnv + yolobox.lib.shell";
      };
    };
}
