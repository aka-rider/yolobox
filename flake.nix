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
      # VERSION is the single source of truth for the release version; CI
      # asserts the git tag matches it. fileContents strips the trailing
      # newline, which is what YO_VERSION wants.
      version = nixpkgs.lib.fileContents ./VERSION;
      forAllSystems = nixpkgs.lib.genAttrs [ "aarch64-darwin" "aarch64-linux" ];
      # The guest account must mirror whichever host user provisioned it
      # (lima's cidata names it after the Mac account, uid-matched to that
      # Mac account's uid) — there is no such thing as a "yolobox user" of
      # its own. Nix has no pure way to learn that name, so it's threaded
      # in impurely: `yo` sets YOLOBOX_USERNAME=$(id -un) on every
      # nixos-rebuild invocation (see cmd_t3/usage's --impure hints in yo).
      username =
        let u = builtins.getEnv "YOLOBOX_USERNAME";
        in if u == "" then
          throw "YOLOBOX_USERNAME is not set — build with --impure and YOLOBOX_USERNAME=$(id -un) (yo does this for you)"
        else u;
      # Unlike username, this is a plain constant: nothing outside this
      # flake creates the agent's account (lima creates only the
      # operator's, from cidata), so there is no host value to thread in.
      agentUser = "agent";
    in {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          yolobox = pkgs.callPackage ./nix/pkgs/yolobox.nix { src = self; inherit version; };
        in {
          inherit yolobox;
          default = yolobox;
        });

      nixosConfigurations.yolobox = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        specialArgs = {
          inherit username agentUser;
          yolobox = self.packages.aarch64-linux.yolobox;
        };
        modules = [
          nixos-lima.nixosModules.lima
          ./nix/base.nix
          ./nix/podman.nix
          ./nix/agentic.nix
          ./nix/lsp.nix
        ];
      };
    };
}
