{ pkgs }:

let
  fragments = {
    rust = with pkgs; [ cargo rustc rust-analyzer clippy rustfmt lldb cargo-flamegraph ];
    # py-spy's own test suite fails under aarch64-linux sandboxed builds
    # (thread-name/line-number assertions sensitive to host scheduling); the
    # built binary itself works, so skip checks rather than drop the profiler.
    python = with pkgs; [ python3 uv pyright python3Packages.debugpy (py-spy.overrideAttrs (_: { doCheck = false; })) ];
    node = with pkgs; [ nodejs typescript typescript-language-server ];
    go = with pkgs; [ go gopls delve graphviz ];
    # gcc leads the PATH so `cc`/`gdb` stay a matched pair; clang-tools only
    # adds clangd/clang-format/clang-tidy, not a competing `clang` binary.
    cxx = with pkgs; [ gcc gnumake cmake pkg-config gdb clang-tools valgrind ];
    postgres = with pkgs; [ postgresql ];
  };

  devShells = builtins.mapAttrs (_: ps: pkgs.mkShell { packages = ps; }) fragments;

  shell = names: pkgs.mkShell {
    packages = builtins.concatMap (n: fragments.${n}) names;
  };
in
{
  inherit devShells shell;
}
