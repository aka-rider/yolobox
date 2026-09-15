{
  description = "yolobox: NixOS VM devbox for AI agents";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs.git?ref=nixos-unstable";
    nixos-lima = {
      url = "git+https://github.com/nixos-lima/nixos-lima.git";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Pinned to a release tag, not a branch: `nix flake update` moves a
    # branch input onto whatever main happens to hold, which for a tool
    # the box ships is an unreleased commit. Bump the tag to upgrade.
    rune = {
      url = "github:aka-rider/rune/v1.4.0";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-lima, rune }:
    let
      # The release tag is the single source of truth for the version.
      # The release workflow writes .version from the tag and moves the
      # tag onto that commit, so this reads the version straight out of
      # the tagged tree. fileContents strips the trailing newline, which
      # is what YO_VERSION wants.
      version = nixpkgs.lib.fileContents ./.version;
      forAllSystems = nixpkgs.lib.genAttrs [ "aarch64-darwin" "aarch64-linux" "x86_64-linux" ];
      # The guest account must mirror whichever host user provisioned it
      # (lima's cidata names it after the Mac account, uid-matched to that
      # Mac account's uid) — there is no such thing as a "yolobox user" of
      # its own. Nix has no pure way to learn that name, so it's threaded
      # in impurely: `yo` sets YOLOBOX_USERNAME=$(id -un) on every
      # nixos-rebuild invocation (see yo's rebuild hint and usage text).
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
        # The guest is always Linux, but its cpu follows whatever machine is
        # evaluating this flake, not a hard-coded one: inside the guest that
        # is the guest's own cpu, and on the host running the documented
        # `nix flake check --impure` it is the host's — both of which is
        # exactly what YOLOBOX_USERNAME above already needs --impure for, so
        # this rides the same impurity rather than adding a new one. A bare
        # `builtins.currentSystem` would hand nixosSystem "aarch64-darwin" on
        # a Mac; `elaborate` splits out the cpu and this pins the kernel to
        # "linux" regardless of which host os is evaluating.
        system = "${(nixpkgs.lib.systems.elaborate builtins.currentSystem).parsed.cpu.name}-linux";
        specialArgs = {
          inherit username agentUser version;
        };
        modules = [
          { nixpkgs.overlays = [ (final: prev: { rune = rune.packages.${prev.stdenv.hostPlatform.system}.rune; }) ]; }
          # nixpkgs' pin still carries herdr 0.8.2 while the Mac is on 0.9.0,
          # and a saved SSH machine needs the guest server's
          # endpoint_protocol_generation to equal the client's exactly. The
          # published linux-aarch64/linux-x86_64 assets are statically linked
          # (no PT_INTERP, no PT_DYNAMIC), so they run here unpatched.
          { yolobox.harness.herdr = {
              version = "0.9.0";
              hash = {
                aarch64-linux = "sha256-nI2yD7fnQnsTjVNnET8WIf/TGfL2XW8AniWUApEV8NI=";
                x86_64-linux = "sha256-T6GgEVjdgEPaktMbJweAsNzBBgMDjZthysTYGrY/tx8=";
              };
            };
          }
          nixos-lima.nixosModules.lima
          ./nix/base.nix
          ./nix/podman.nix
          ./nix/agentic.nix
          ./nix/lsp.nix
          ./nix/guest.nix
        ];
      };
    };
}
