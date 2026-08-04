#!/usr/bin/env bash
# zoxide -> /usr/local/bin (installer supports --bin-dir).
TOOL_NAME=zoxide
TOOL_SOURCES=( "https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh" )
TOOL_SMOKE=( "zoxide --version" )

tool_install() {
    curl -sSfL https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh \
        | sh -s -- --bin-dir /usr/local/bin
}
