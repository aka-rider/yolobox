#!/usr/bin/env bash
# Context7 MCP server — keyless stdio server for library docs.
TOOL_NAME=context7-mcp
TOOL_VERSION=3.2.5
TOOL_SOURCES=( "https://registry.npmjs.org/@upstash/context7-mcp" )
TOOL_SMOKE=( "context7-mcp --help" )

tool_install() {
    # NPM_CONFIG_PREFIX=/usr/local (Dockerfile) puts the bin at
    # /usr/local/bin/context7-mcp.
    npm install -g "@upstash/context7-mcp@${TOOL_VERSION}"
}
