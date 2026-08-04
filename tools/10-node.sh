#!/usr/bin/env bash
# Node.js 24 LTS via the NodeSource apt repo.
TOOL_NAME=node
TOOL_APT=(nodejs)
TOOL_ENV=( 'NPM_CONFIG_PREFIX=/usr/local' )
TOOL_HOME_DIRS=( .npm )
TOOL_SOURCES=(
    "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
    "https://deb.nodesource.com/node_24.x/dists/nodistro/Release"
)
TOOL_SMOKE=(
    "node --version"
    "npm --version"
)

tool_apt_repo() {
    yb_apt_repo nodesource \
        "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key" \
        "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main"
}
