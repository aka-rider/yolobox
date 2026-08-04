#!/usr/bin/env bash
# git-delta — .deb from GitHub releases, installed via apt so its dependencies
# resolve. Deliberately does NOT rm -rf /var/lib/apt/lists — install-all.sh's
# final cleanup step owns that, after phase 3: Playwright's --with-deps
# repopulates the lists later, so clearing them here would just be redone.
TOOL_NAME=git-delta
TOOL_VERSION=0.18.2
TOOL_SOURCES=( "https://github.com/dandavison/delta/releases/download/${TOOL_VERSION}/git-delta_${TOOL_VERSION}_$(yb_arch_deb).deb" )
TOOL_SMOKE=( "delta --version" )

tool_install() {
    curl -fsSL "https://github.com/dandavison/delta/releases/download/${TOOL_VERSION}/git-delta_${TOOL_VERSION}_$(yb_arch_deb).deb" \
        -o /tmp/git-delta.deb \
    && apt-get install -y --no-install-recommends /tmp/git-delta.deb \
    && rm /tmp/git-delta.deb
}
