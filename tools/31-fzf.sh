#!/usr/bin/env bash
# fzf cloned to /opt/fzf (NOT ~). --bin fetches only the binary into
# /opt/fzf/bin, which is already on PATH (FZF_DIR=/opt/fzf).
TOOL_NAME=fzf
TOOL_SOURCES=( "https://github.com/junegunn/fzf.git" )
TOOL_SMOKE=( "fzf --version" )
TOOL_ENV=( 'FZF_DIR=/opt/fzf' )
TOOL_PATH=( '/opt/fzf/bin' )

tool_install() {
    git clone --depth 1 https://github.com/junegunn/fzf.git "${FZF_DIR}" \
        && "${FZF_DIR}/install" --bin \
        && chmod -R a+rX "${FZF_DIR}"
}
