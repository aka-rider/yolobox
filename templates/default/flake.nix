{
  description = "yolobox project dev shell (override `inputs.yolobox.url` to pin a different yolobox checkout)";

  inputs = {
    # Explicit "git+file://" rather than a bare path: since Nix 2.26 a bare
    # absolute path as a flake input infers "path:", which copies the whole
    # ~/wrk/yolobox tree, including .git, on every lock.
    yolobox.url = "git+file:///home/xiii.guest/wrk/yolobox";
  };

  outputs = { self, yolobox }:
    {
      # Fragment menu: the attribute names in yolobox's nix/shells/default.nix.
      #
      # aarch64-linux is deliberate, not a stand-in for flake-utils: yolobox's
      # flake pins `import nixpkgs { system = "aarch64-linux"; }` and lib.shell
      # closes over that pkgs, so it can only ever produce an aarch64-linux
      # derivation. flake-utils here would advertise systems it cannot build.
      devShells.aarch64-linux.default = yolobox.lib.shell [ "rust" "postgres" ];
    };
}
