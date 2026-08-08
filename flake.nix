{
  description = "yolobox: NixOS VM devbox for AI agents";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs.git?ref=nixos-unstable";
    nixos-lima = {
      url = "git+https://github.com/nixos-lima/nixos-lima.git";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-lima }: {
    nixosConfigurations.yolobox = nixpkgs.lib.nixosSystem {
      system = "aarch64-linux";
      modules = [
        nixos-lima.nixosModules.lima
        ./nix/base.nix
        ./nix/podman.nix
        ./nix/agentic.nix
      ];
    };
  };
}
