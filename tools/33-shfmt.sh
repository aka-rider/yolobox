#!/usr/bin/env bash
# shfmt — single static binary from GitHub releases.
TOOL_NAME=shfmt
TOOL_VERSION=3.8.0
TOOL_SOURCES=( "https://github.com/mvdan/sh/releases/download/v${TOOL_VERSION}/shfmt_v${TOOL_VERSION}_linux_$(yb_arch_deb)" )
TOOL_SMOKE=( "shfmt --version" )

tool_install() {
    yb_fetch_bin \
        "https://github.com/mvdan/sh/releases/download/v${TOOL_VERSION}/shfmt_v${TOOL_VERSION}_linux_$(yb_arch_deb)" \
        shfmt
}
