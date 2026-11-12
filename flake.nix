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
      # The one spelling of every literal yo, the box and the scripts share.
      facts = builtins.fromJSON (builtins.readFile ./facts.json);
      forAllSystems = nixpkgs.lib.genAttrs ([ "aarch64-darwin" ] ++ linuxSystems);
      # The guest account must mirror whichever host user provisioned it
      # (lima's cidata names it after the Mac account, uid-matched to that
      # Mac account's uid) — there is no such thing as a "yolobox user" of
      # its own. Nix has no pure way to learn that name, so it's threaded
      # in impurely: `yo` sets YOLOBOX_USERNAME=$(id -un) on every
      # nixos-rebuild invocation (see yo's rebuild hint and usage text).
      username =
        let u = builtins.getEnv facts.nix_env_username;
        in if u == "" then
          throw "${facts.nix_env_username} is not set — build with --impure and ${facts.nix_env_username}=$(id -un) (yo does this for you)"
        # Checked here, not as module assertions: either name makes two
        # users.users definitions conflict, which throws before any assertion.
        else if u == facts.agent_user then
          throw "${facts.nix_env_username} is \"${u}\", the agent's own account name — the operator and the agent must be different accounts"
        else if u == "root" then
          throw "${facts.nix_env_username} is root — the operator mirrors the Mac user, never root"
        else u;
      # The box's hostname is the one input that is optional: unset or empty
      # means "yolobox", so CI and a bare `nix eval` of the box evaluate
      # without it. `yo` sets it per machine (-m NAME gives yolobox-NAME). A
      # set value must be one RFC 1123 label, lowercase, because it reaches
      # the kernel and DHCP, and any mesh VPN's node name.
      hostname =
        let n = builtins.getEnv facts.nix_env_hostname;
        in if n == "" then facts.default_instance
        else if builtins.match facts.hostname_label_re n != null then n
        else throw "${facts.nix_env_hostname} is \"${n}\" — it must be one DNS label: lowercase letters, digits and '-', 1 to 63 characters, not starting or ending with '-' (leave it unset for \"${facts.default_instance}\")";
      # The flake ref the box is built from, a host fact like username: `yo
      # bootstrap` sets YOLOBOX_FLAKE_REF to the exact locked tree it builds
      # (github:OWNER/REPO/<rev>?narHash=<hash>) and base.nix stamps it
      # verbatim into /etc/yolobox/flake-ref, which `yolobox-guest rebuild`
      # builds from again. It is required, because a rebuild without it would
      # delete the stamp. yo validates the ref's grammar on the host.
      flakeRef =
        let r = builtins.getEnv facts.nix_env_flake_ref;
        in if r == "" then
          throw "${facts.nix_env_flake_ref} is not set — set it to the flake ref you build from and build with --impure (yo does this for you)"
        else r;
      guestSystem =
        let s = builtins.currentSystem;
        in if builtins.elem s linuxSystems then s
        else throw "the box builds for aarch64-linux or x86_64-linux, not ${s} — pass --eval-system aarch64-linux (Apple silicon) or --eval-system x86_64-linux (Intel Mac) when evaluating on a Mac";
    in {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          # self.rev exists only for a clean git tree (a fetched github: ref
          # always is one); a dirty tree builds a yo that keeps "dev" there
          # and so builds its release tag, like a brew yo.
          yolobox = pkgs.callPackage ./nix/pkgs/yolobox.nix {
            src = self;
            inherit version;
            sourceRev = self.rev or "dev";
          };
        in {
          inherit yolobox;
          default = yolobox;
        });

      # builtins.seq forces username, hostname and flakeRef here (guestSystem
      # is forced by `system`), so evaluating the box fails at once when one is
      # unset or invalid, instead of only when some module happens to read it.
      nixosConfigurations.yolobox = builtins.seq username (builtins.seq hostname (builtins.seq flakeRef (nixpkgs.lib.nixosSystem {
        system = guestSystem;
        specialArgs = {
          inherit username version hostname flakeRef facts;
        };
        modules = [
          nixos-lima.nixosModules.lima
          ./nix/lib/args.nix
          ./nix/base.nix
          ./nix/swap.nix
          ./nix/luks.nix
          ./nix/podman.nix
          ./nix/harnesses.nix
          ./nix/herd-server.nix
          ./nix/display.nix
          ./nix/lsp.nix
          ./nix/guest.nix
          ./nix/egress.nix
          ./nix/hardening.nix
        ];
      })));
    };
}
