#!/usr/bin/env bash
# uv -> /usr/local/bin (UV_INSTALL_DIR). Do not let it edit shell rc files.
# The installer honours UV_INSTALL_DIR.
TOOL_NAME=uv
TOOL_ENV=( 'UV_INSTALL_DIR=/usr/local/bin' )
TOOL_SOURCES=( "https://astral.sh/uv/install.sh" )
TOOL_SMOKE=( "uv --version" )

tool_install() {
    curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh
}
