#!/usr/bin/env bash
# Go toolchain -> /usr/local/go (GOROOT).
TOOL_NAME=go
TOOL_VERSION=1.26.5
TOOL_SOURCES=(
    "https://go.dev/dl/go${TOOL_VERSION}.linux-$(yb_arch_go).tar.gz"
)
TOOL_SMOKE=( "go version" )
TOOL_ENV=( 'GOROOT=/usr/local/go' 'GOPATH=@HOME@/go' )
TOOL_PATH=( /usr/local/go/bin )
TOOL_HOME_DIRS=( go/pkg/mod )

tool_install() {
    curl -fsSL "https://go.dev/dl/go${TOOL_VERSION}.linux-$(yb_arch_go).tar.gz" -o /tmp/go.tar.gz \
        && rm -rf /usr/local/go \
        && tar -C /usr/local -xzf /tmp/go.tar.gz \
        && rm /tmp/go.tar.gz
}
