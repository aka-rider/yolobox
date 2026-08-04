#!/usr/bin/env bash
# yq — single static binary from GitHub releases.
TOOL_NAME=yq
TOOL_VERSION=4.44.3
TOOL_SOURCES=( "https://github.com/mikefarah/yq/releases/download/v${TOOL_VERSION}/yq_linux_$(yb_arch_deb)" )
TOOL_SMOKE=( "yq --version" )

tool_install() {
    yb_fetch_bin \
        "https://github.com/mikefarah/yq/releases/download/v${TOOL_VERSION}/yq_linux_$(yb_arch_deb)" \
        yq
}
