{
  description = "yolobox: NixOS VM devbox for AI agents";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs.git?ref=nixos-unstable";
    nixos-lima = {
      url = "git+https://github.com/nixos-lima/nixos-lima.git";
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
