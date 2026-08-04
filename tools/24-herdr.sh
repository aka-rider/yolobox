#!/usr/bin/env bash
# herdr — installer honours HERDR_INSTALL_DIR, so the binary lands directly on
# a system path rather than under $HOME, which the home volume would mask at
# runtime.
TOOL_NAME=herdr
TOOL_ENV=( 'HERDR_INSTALL_DIR=/usr/local/bin' )
TOOL_SOURCES=( "https://herdr.dev/install.sh" )
TOOL_SMOKE=( "herdr --version" )

tool_install() {
    curl -fsSL https://herdr.dev/install.sh | sh
}
