{
  description = "yolobox: NixOS VM devbox for AI agents";

  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs.git?ref=nixos-unstable";
    nixos-lima = {
      url = "git+https://github.com/nixos-lima/nixos-lima.git";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.nixpkgs-unstable.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-lima }:
    let
      # The release tag is the single source of truth for the version.
      # The release workflow writes .version from the tag and moves the
      # tag onto that commit, so this reads the version straight out of
      # the tagged tree. fileContents strips the trailing newline, which
      # is what YO_VERSION wants.
      version = nixpkgs.lib.fileContents ./.version;
      # The guest's CPU follows the host's (Lima/vz on an Apple-silicon Mac
      # runs aarch64, libvirt/KVM on a Linux PC runs x86_64), so the box
      # evaluates for both Linux systems.
      linuxSystems = [ "aarch64-linux" "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs ([ "aarch64-darwin" ] ++ linuxSystems);
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
      # Which OS runs the VM (and so which hypervisor stack the guest was
      # provisioned by) is a host fact like username: `yo` sets
      # YOLOBOX_HOST_OS on every nixos-rebuild invocation. It is separate
      # from the CPU architecture, which guestSystem reads from the nix
      # that is evaluating.
      hostOs =
        let h = builtins.getEnv "YOLOBOX_HOST_OS";
        in if h == "darwin" || h == "linux" then h
        else throw "YOLOBOX_HOST_OS is \"${h}\" — set it to darwin (Lima on a Mac) or linux (libvirt on a Linux PC) and build with --impure (yo does this for you)";
      # The box's hostname, and with it the tailnet node name, is the one
      # input that is optional: unset or empty means "yolobox", so CI, a bare
      # `nix flake check` and every box built before this variable existed
      # evaluate unchanged. `yo` sets it per machine (-m NAME gives
      # yolobox-NAME). A set value must be one RFC 1123 label, lowercase,
      # because it reaches the kernel, DHCP and tailscale.
      hostname =
        let n = builtins.getEnv "YOLOBOX_HOSTNAME";
        in if n == "" then "yolobox"
        else if builtins.match "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?" n != null then n
        else throw "YOLOBOX_HOSTNAME is \"${n}\" — it must be one DNS label: lowercase letters, digits and '-', 1 to 63 characters, not starting or ending with '-' (leave it unset for \"yolobox\")";
      guestSystem =
        let s = builtins.currentSystem;
        in if builtins.elem s linuxSystems then s
        else throw "the box builds for aarch64-linux or x86_64-linux, not ${s} — pass --system aarch64-linux when evaluating on a Mac";
    in {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          yolobox = pkgs.callPackage ./nix/pkgs/yolobox.nix { src = self; inherit version; };
        in {
          inherit yolobox;
          default = yolobox;
        }
        # Linux-only, so `nix flake check` on a Mac never evaluates systemd or
        # cryptsetup for darwin.
        // nixpkgs.lib.optionalAttrs (builtins.elem system linuxSystems) (
          let apps = import ./nix/luks/apps.nix { inherit pkgs; systemd = pkgs.systemd; };
          in {
            yolobox-luks-migrate = apps.migrate;
            yolobox-unlock = apps.unlock;
            yolobox-initrd-authorized-keys = apps.keys;
          }
        ));

      # builtins.seq forces hostOs and hostname here (guestSystem is forced by
      # `system`), so evaluating the box fails at once when one is unset or
      # invalid, instead of only when some module happens to read it.
      nixosConfigurations.yolobox = builtins.seq hostOs (builtins.seq hostname (nixpkgs.lib.nixosSystem {
        system = guestSystem;
        specialArgs = {
          inherit username agentUser version hostOs hostname;
        };
        modules = [
          # nixpkgs' pin can lag herdr, and a saved SSH machine needs the
          # guest server's endpoint_protocol_generation to equal the client's
          # exactly. The pinned release must therefore speak the client's
          # endpoint protocol generation; bump version and hash together when
          # the client moves to a new generation. The published
          # linux-aarch64 asset is statically linked (no PT_INTERP, no
          # PT_DYNAMIC) and the linux-x86_64 one is a static-PIE (no
          # PT_INTERP, no DT_NEEDED), so both run here unpatched.
          { yolobox.harness.herdr = {
              version = "0.9.5";
              hashes = {
                aarch64-linux = "sha256-F9dTby2lCFlSNoDvJ53/ysjkojX05G/o2mwuxEFBiXw=";
                x86_64-linux = "sha256-FNnovItKngEY13wyJm921GHwm0A0LrI3UpYL2J9Zkp0=";
              };
            };
          }
          nixos-lima.nixosModules.lima
          ./nix/base.nix
          ./nix/swap.nix
          ./nix/luks.nix
          ./nix/podman.nix
          ./nix/agentic.nix
          ./nix/lsp.nix
          ./nix/guest.nix
          ./nix/tailscale.nix
          ./nix/egress.nix
          ./nix/hardening.nix
        ];
      }));
    };
}
