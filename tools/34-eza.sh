#!/usr/bin/env bash
# eza — tarball from GitHub releases, named with the rust target triple.
TOOL_NAME=eza
TOOL_VERSION=0.20.0
TOOL_SOURCES=( "https://github.com/eza-community/eza/releases/download/v${TOOL_VERSION}/eza_$(yb_arch_rust).tar.gz" )
TOOL_SMOKE=( "eza --version" )

tool_install() {
    yb_fetch_tar_bin \
        "https://github.com/eza-community/eza/releases/download/v${TOOL_VERSION}/eza_$(yb_arch_rust).tar.gz" \
        eza
}
