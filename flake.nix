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
      # The guest's CPU follows the host's: Lima/vz runs the Mac's own CPU
      # (aarch64 on Apple silicon, x86_64 on Intel), libvirt/KVM on a Linux PC
      # runs x86_64. So the box evaluates for both Linux systems. yo itself has
      # no x86_64-darwin package: nixpkgs dropped that platform in 26.11, so an
      # Intel Mac installs yo through brew.
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
      # The guest is never told which OS runs it: each setting that differs
      # between hosts gates on the fact it needs (nix/base.md, "No host OS in
      # the guest"). An older yo still passes the host OS; nothing reads it.
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
      # The flake ref the box is built from, a host fact like username: `yo`
      # (or `yolobox-guest rebuild`, from the stamp) sets YOLOBOX_FLAKE_REF on
      # every nixos-rebuild, and base.nix stamps it into /etc/yolobox/flake-ref
      # so a later rebuild from inside the box builds from the same tree. It is
      # required, because a rebuild without it would delete the stamp and
      # leave the next in-box rebuild nothing to build from.
      # It must be exactly github:OWNER/REPO/REF (release.GITHUB_FLAKE_RE),
      # because it names the tree the box rebuilds from.
      flakeRef =
        let r = builtins.getEnv "YOLOBOX_FLAKE_REF";
        in if builtins.match "github:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9][A-Za-z0-9._-]*" r != null then r
        else throw "YOLOBOX_FLAKE_REF is \"${r}\" — set it to the flake ref you build from, github:OWNER/REPO/REF, and build with --impure (yo does this for you)";
      guestSystem =
        let s = builtins.currentSystem;
        in if builtins.elem s linuxSystems then s
        else throw "the box builds for aarch64-linux or x86_64-linux, not ${s} — pass --system aarch64-linux (Apple silicon) or --system x86_64-linux (Intel Mac) when evaluating on a Mac";
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

      # builtins.seq forces hostname and flakeRef here (guestSystem is forced
      # by `system`), so evaluating the box fails at once when one is unset or
      # invalid, instead of only when some module happens to read it.
      nixosConfigurations.yolobox = builtins.seq hostname (builtins.seq flakeRef (nixpkgs.lib.nixosSystem {
        system = guestSystem;
        specialArgs = {
          inherit username agentUser version hostname flakeRef;
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
