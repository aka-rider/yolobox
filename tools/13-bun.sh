#!/usr/bin/env bash
# bun -> /usr/local/bun (BUN_INSTALL). Binary lands at $BUN_INSTALL/bin/bun.
# The installer honours BUN_INSTALL.
TOOL_NAME=bun
TOOL_SOURCES=( "https://bun.com/install" )
TOOL_SMOKE=( "bun --version" )
TOOL_ENV=( 'BUN_INSTALL=/usr/local/bun' )
TOOL_PATH=( /usr/local/bun/bin )
TOOL_HOME_DIRS=( .bun/install/cache )

tool_install() {
    curl -fsSL https://bun.com/install | bash \
        && chmod -R a+rX "${BUN_INSTALL}"
}
