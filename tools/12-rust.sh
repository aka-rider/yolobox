#!/usr/bin/env bash
# Rust via rustup. CARGO_HOME/RUSTUP_HOME are system paths (baked into the
# image's generated ENV region, from TOOL_ENV below); --no-modify-path because
# PATH already carries /usr/local/cargo/bin. World-readable so the
# unprivileged runtime user can execute the toolchain.
#
# CARGO_HOME appears twice, deliberately, at two different scopes:
#   - BUILD time (TOOL_ENV, below): /usr/local/cargo, a system path, so the
#     cargo/rustc binaries land on PATH rather than under the user's home --
#     that home directory is masked at runtime by the /home named volume, so
#     anything installed there at build time would be invisible once the
#     container runs.
#   - RUN time (TOOL_RUNTIME_ENV, below): re-pointed at @HOME@/.cargo so the
#     registry/git cache persists in that volume across container recreation.
# That runtime repoint would otherwise also redirect `cargo install`'s
# *binary* output to @HOME@/.cargo/bin -- a path on neither PATH nor
# TOOL_HOME_DIRS. CARGO_INSTALL_ROOT=/usr/local pins installed binaries to
# /usr/local/bin instead (on PATH, box-user-writable), because
# CARGO_INSTALL_ROOT takes precedence over CARGO_HOME for that one purpose.
# Without it, `cargo install ripgrep` succeeds and produces an unreachable
# binary, silently.
TOOL_NAME=rust
TOOL_SOURCES=( "https://sh.rustup.rs" )
TOOL_SMOKE=(
    "cargo --version"
    "rustc --version"
)
TOOL_ENV=( 'CARGO_HOME=/usr/local/cargo' 'RUSTUP_HOME=/usr/local/rustup' )
TOOL_PATH=( /usr/local/cargo/bin )
TOOL_HOME_DIRS=( .cargo/registry .cargo/git )
TOOL_RUNTIME_ENV=( 'CARGO_HOME=@HOME@/.cargo' 'CARGO_INSTALL_ROOT=/usr/local' )

tool_install() {
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --default-toolchain stable \
        && chmod -R a+rX "${CARGO_HOME}" "${RUSTUP_HOME}"
}
