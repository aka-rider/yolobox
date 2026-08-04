#!/usr/bin/env bash
# Shared helpers for tools/*.sh modules.
#
# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------
# Each tools/NN-name.sh sets variables and defines up to two functions:
#
# | Symbol           | Kind               | Meaning                                            |
# |-------------------|--------------------|----------------------------------------------------|
# | TOOL_NAME         | var, required      | short id, used in log lines                        |
# | TOOL_VERSION      | var, optional      | pinned version; empty = floats                     |
# | TOOL_APT          | array, optional    | apt package names, merged into ONE `apt-get install` |
# | TOOL_SOURCES      | array, optional    | external URL(s) this module downloads from         |
# | TOOL_SMOKE        | array, optional    | commands run as the box user to prove the tool resolves |
# | tool_prepare()    | fn, optional       | runs FIRST, before any repo registration. Only 02-user.sh uses it |
# | tool_apt_repo()   | fn, optional       | registers an apt repo; runs BEFORE the apt batch   |
# | tool_install()    | fn, optional       | everything else; runs AFTER the apt batch, in filename order |
#
# `tool_prepare()` exists for exactly one reason: the box user must exist
# before `tool_apt_repo()` runs. `ENV HOME=/home/${USERNAME}` is live
# image-wide from the first layer, but `ubuntu:26.04` ships only
# `/home/ubuntu`, so until `useradd -m` runs, `$HOME` points at a directory
# that does not exist -- and `yb_apt_repo()`'s `gpg --dearmor` wants to create
# `$HOME/.gnupg`. The Dockerfile historically had this order already (user
# creation before repo registration); a naive "user creation is just another
# tool_install" would invert it and break the first repo module. As
# belt-and-braces, install-all.sh also exports GNUPGHOME=/tmp/gnupg for the
# duration.
#
# Modules are sourced, must be idempotent, and must not rely on a previous
# module's state -- install-all.sh unsets all TOOL_* and runs
# `unset -f tool_prepare tool_apt_repo tool_install` between modules.
# ---------------------------------------------------------------------------
set -uo pipefail

# Fixed system PATH tail, appended after every module's TOOL_PATH entries.
# The single shared copy: tools/build.sh (Dockerfile ENV-region generator)
# and tools/install-all.sh (in-image drift guard) both source lib.sh already,
# so both read this one definition instead of carrying byte-identical copies.
readonly YB_FIXED_PATH_TAIL="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

# Architecture mapping — the single source of truth.
# NOTE: tools/build.sh sources this ON THE macOS HOST, where dpkg does
# not exist — hence the uname fallback.
yb_arch_deb() {
    if command -v dpkg >/dev/null 2>&1; then
        dpkg --print-architecture
    else
        case "$(uname -m)" in
            arm64|aarch64) echo arm64 ;;
            x86_64|amd64)  echo amd64 ;;
            *) echo "unsupported arch: $(uname -m)" >&2; return 1 ;;
        esac
    fi
}
yb_arch_go() { yb_arch_deb; }
yb_arch_rust() {
    case "$(yb_arch_deb)" in
        amd64) echo x86_64-unknown-linux-gnu ;;
        arm64) echo aarch64-unknown-linux-gnu ;;
        *) return 1 ;;
    esac
}
yb_arch_node() {
    case "$(yb_arch_deb)" in
        amd64) echo x64 ;;
        arm64) echo arm64 ;;
        *) return 1 ;;
    esac
}

# Download one static binary into /usr/local/bin and make it executable.
yb_fetch_bin() { # <url> <dest-name>
    curl -fsSL "$1" -o "/usr/local/bin/$2" && chmod 0755 "/usr/local/bin/$2"
}

# Download a .tar.gz whose payload is bare executable(s) at the archive root,
# straight onto PATH. Same idea as yb_fetch_bin above, one archive layer up.
# Verified layouts: eza ships `./eza`, opencode ships `opencode` — both flat.
yb_fetch_tar_bin() { # <url> <bin-name>...
    local url="$1"; shift
    local tmp b rc
    tmp="$(mktemp)" || return 1
    curl -fsSL "$url" -o "$tmp" && tar -C /usr/local/bin -xzf "$tmp"
    rc=$?
    rm -f "$tmp"
    [ "$rc" -eq 0 ] || return "$rc"
    for b in "$@"; do chmod 0755 "/usr/local/bin/${b}" || return 1; done
}

# Register a third-party apt repo: dearmor a key, write a sources list.
yb_apt_repo() { # <name> <key-url> <deb-line>
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL "$2" | gpg --dearmor -o "/etc/apt/keyrings/$1.gpg"
    printf '%s\n' "$3" > "/etc/apt/sources.list.d/$1.list"
}

# The module-iteration chokepoint. Every consumer that needs to walk
# tools/NN-name.sh in order (install-all.sh, smoke.sh, build.sh, and
# mcp/render.sh) calls this instead of hand-rolling its own
# glob + reset + source loop.
#
# The module directory is derived from lib.sh's OWN location, never
# hardcoded -- lib.sh sits beside the modules in both the repo and the image,
# so this resolves correctly whether the caller is running on the macOS host
# (tools/build.sh) or inside the build (install-all.sh, smoke.sh).
# Hardcoding /opt/yolobox/tools here would make every host-side caller iterate
# ZERO modules -- a silently PASSING empty run.
#
# Per module, before sourcing: every TOOL_* variable is unset via ${!TOOL_*}
# (not a hardcoded list -- the contract keeps growing) and every contract
# function is unset, including tool_configure/tool_seed which no module
# defines yet -- unsetting them now keeps this list complete as later waves
# add them.
#
# Runs in the CALLER'S shell, no subshell/pipeline: consumers accumulate
# globals across iterations (e.g. install-all.sh's ALL_APT, smoke.sh's
# ALL_SMOKE) and a subshell would yield empty arrays -- a passing-but-empty
# run.
yb_each_module() { # <callback-function-name>
    local callback="$1"
    local moddir
    moddir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

    # Glob in LC_ALL=C order, matching the module filename-prefix bands.
    local had_lc_all=0 old_lc_all=""
    if [ -n "${LC_ALL+x}" ]; then had_lc_all=1; old_lc_all="$LC_ALL"; fi
    LC_ALL=C
    local -a mods=( "${moddir}"/[0-9][0-9]-*.sh )
    if [ "$had_lc_all" -eq 1 ]; then LC_ALL="$old_lc_all"; else unset LC_ALL; fi

    local m var
    for m in "${mods[@]}"; do
        for var in ${!TOOL_*}; do unset "$var"; done
        unset -f tool_prepare tool_apt_repo tool_install tool_configure tool_seed 2>/dev/null || true
        # shellcheck disable=SC1090
        source "$m"
        "$callback" "$m"
    done
}
