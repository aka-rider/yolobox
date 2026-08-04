#!/usr/bin/env bash
# Playwright MCP server + in-box Chromium.
TOOL_NAME=playwright-mcp
TOOL_VERSION=0.0.78
TOOL_SOURCES=( "https://registry.npmjs.org/@playwright/mcp" )
TOOL_SMOKE=( "playwright-mcp --version" )
# PLAYWRIGHT_BROWSERS_PATH is deliberately outside the box user's home
# directory: the /home volume would otherwise mask ~1 GB of Chromium and it
# would get re-copied into the volume on every fresh container. No
# TOOL_PATH -- the MCP binary lands in /usr/local/bin via NPM_CONFIG_PREFIX,
# in the fixed system tail.
TOOL_ENV=( 'PLAYWRIGHT_BROWSERS_PATH=/var/lib/ms-playwright' )

tool_install() {
    npm install -g "@playwright/mcp@${TOOL_VERSION}"
    # Use the EXACT playwright-core that @playwright/mcp resolves, rather than
    # guessing a matching `playwright` package version. playwright-core's own
    # package.json "exports" map does NOT list "./cli.js" as a subpath (only
    # ".", "./package.json" and a few "./lib/*" paths are exported), so
    # `require.resolve('playwright-core/cli.js', ...)` throws
    # ERR_PACKAGE_PATH_NOT_EXPORTED. "./package.json" IS exported, and
    # playwright-core's bin field ("playwright-core": "cli.js") places cli.js
    # directly beside it — resolve package.json, then swap the filename with a
    # plain string op (a filesystem path join, not a module resolution, so the
    # exports map does not apply).
    local pw_pkg_json pw_cli
    pw_pkg_json="$(node -e "console.log(require.resolve('playwright-core/package.json',{paths:['/usr/local/lib/node_modules/@playwright/mcp']}))")"
    pw_cli="${pw_pkg_json%package.json}cli.js"
    # PLAYWRIGHT_BROWSERS_PATH=/var/lib/ms-playwright is set image-wide via
    # this module's TOOL_ENV — rely on it, do not set it here.
    node "${pw_cli}" install --with-deps chromium
    chmod -R a+rX /var/lib/ms-playwright
}
