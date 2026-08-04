#!/usr/bin/env bash
# Base apt packages + the two Debian-rename symlinks.
TOOL_NAME=base
TOOL_APT=(
    zsh
    git
    openssh-client
    openssh-server
    ca-certificates
    gnupg
    curl
    wget
    unzip
    ripgrep
    locales
    build-essential
    pkg-config
    gcc
    g++
    cmake
    llvm
    clang
    lld
    gh
    jq
    tmux
    direnv
    bat
    fd-find
    sqlite3
    shellcheck
    postgresql-client
    # The image ships neither `ss` nor `netstat` otherwise, and a later
    # (herd sshd) gate needs `ss`.
    iproute2
)
# Generic $HOME-relative cache/state dirs owned by no language runtime (the
# go/cargo/npm/bun ones belong to their own modules). These live here rather
# than being baked into the image because an empty named volume is seeded
# from the image exactly once. Bare-relative, without the HOME placeholder
# used by other TOOL_* arrays — yolobox-init prefixes $HOME itself.
TOOL_HOME_DIRS=( .cache .local/state .local/bin )
TOOL_SMOKE=(
    "bat --version"
    "fd --version"
    "rg --version"
    "jq --version"
    "tmux -V"
    "zsh --version"
    "shellcheck --version"
    "psql --version"
    "direnv --version"
    "cmake --version"
    "sqlite3 --version"
    "wget --version"
    "git --version"
    "clang --version"
    "gh --version"
)

tool_install() {
    # Ubuntu's `bat` package installs the binary as `batcat`, and `fd-find`
    # installs it as `fdfind` — verified: /usr/bin/bat does not exist in the
    # current image. Symlink the expected names onto PATH.
    ln -sf /usr/bin/batcat /usr/local/bin/bat
    ln -sf /usr/bin/fdfind /usr/local/bin/fd
}
