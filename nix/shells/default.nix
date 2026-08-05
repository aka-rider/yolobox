{ pkgs }:

let
  fragments = {
    rust = with pkgs; [ cargo rustc rust-analyzer clippy rustfmt lldb ];
    python = with pkgs; [ python3 uv pyright python3Packages.debugpy ];
    node = with pkgs; [ nodejs typescript typescript-language-server ];
    go = with pkgs; [ go gopls delve ];
    # gcc leads the PATH so `cc`/`gdb` stay a matched pair; clang-tools only
    # adds clangd/clang-format/clang-tidy, not a competing `clang` binary.
    cxx = with pkgs; [ gcc gnumake cmake pkg-config gdb clang-tools ];
    postgres = with pkgs; [ postgresql ];
  };

  devShells = builtins.mapAttrs (_: ps: pkgs.mkShell { packages = ps; }) fragments;

  shell = names: pkgs.mkShell {
    packages = builtins.concatMap (n: fragments.${n}) names;
  };
in
{
  inherit fragments devShells shell;
}
