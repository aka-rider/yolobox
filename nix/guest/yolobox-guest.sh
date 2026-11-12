#!/usr/bin/env bash
# The guest-side half of `yo`: subcommands over ssh, one per account, so the
# find expressions, the gc tiers and the AWS probe are shellchecked at build
# time instead of living in yo's Python string literals where nothing checks
# them. Subcommands: projects, landing-dir,
# gc-machine, gc-user, aws-creds, aws-check, status, and rebuild, the one a
# person runs inside the box rather than `yo`.
#
# writeShellApplication prepends `set -o errexit -o nounset -o pipefail`. The
# gc tiers below were written for a shell with none of that — a cleanup step
# that legitimately finds nothing to do still exits nonzero (podman does),
# and that must not cancel the steps after it. `step` stays safe as-is,
# because errexit does not fire on a command that is the condition of an
# `if`; the few captures that are NOT already inside an `if` are guarded
# individually with `|| true` or an explicit `rc=$?`, right where they are.

# The facts of the box, YOLOBOX_<KEY>=value lines nix renders from facts.json.
# The YOLOBOX_FACTS override is a test seam.
# shellcheck source=/dev/null
. "${YOLOBOX_FACTS:-/etc/yolobox/facts.sh}"

# The subcommands read the words curl, dig, sudo, systemctl and the rest print,
# and yo reads theirs: a translated tool would say something else.
export LC_ALL=C

# The project walk's and the Go-cache walk's pruned directory names: a repo
# inside node_modules is not a project. gc --deep walks these names too, plus
# REMOVED_OUTRIGHT, and `yo cp` downloads drop both lists on the host side.
read -ra BUILD_DIRS <<<"${YOLOBOX_GC_BUILD_DIRS}"

# Directories gc --deep removes with remove_tree, each only if git ignores it.
# They are dependency installs: the project's own tooling recreates them.
read -ra REMOVED_OUTRIGHT <<<"${YOLOBOX_GC_REMOVED_OUTRIGHT}"

# NAME:MANIFEST pairs, as facts.json renders a map: a directory called NAME is build output only when
# MANIFEST sits beside it, because the name alone proves nothing (a `target`
# can hold code or data). Without the manifest gc --deep keeps it and says so.
# A `target` with its Cargo.toml is cleaned by `cargo clean`; a `.next` with
# its package.json is removed outright.
read -ra NEEDS_MANIFEST <<<"${YOLOBOX_BUILD_OUTPUT_MANIFESTS}"

# The one place cache names under ~/.cache are spelled; gc --yes deletes
# them all. Only caches a tool rebuilds or refetches by itself belong here:
# model weights and app data (huggingface, ms-playwright, ...) never do.
LOCAL_CACHES=(go-build gopls goimports golangci-lint staticcheck uv pip pip-tools pnpm zig cargo-zigbuild opengrep nix)

# What `go` writes as the first line of a build cache's README.
GO_BUILD_CACHE_HEADER='This directory holds cached build artifacts from the Go build system.'

# Exits 2 with SUB's usage line unless exactly COUNT arguments follow. Every
# subcommand goes through it, so an argument it does not take is refused
# instead of dropped.
want_args() {
    local sub=$1 usage=$2 count=$3
    shift 3
    if [ "$#" -ne "${count}" ]; then
        printf 'yolobox-guest: %s: usage: %s\n' "${sub}" "${usage}" >&2
        exit 2
    fi
}
# Sets EXPR to a find expression matching any of the given directory names.
build_expr() {
    EXPR=('(')
    local name
    for name in "$@"; do
        [ "${#EXPR[@]}" -gt 1 ] && EXPR+=(-o)
        EXPR+=(-name "${name}")
    done
    EXPR+=(')')
}

GC_FAILED=0
TARGETS_FILE=

# What gc --deep decided to do, filled by deep_plan: roots to `make clean`
# (each with its dry-run text at the same index), `target` directories to
# `cargo clean` from their parent, roots to `go clean`, and directories to
# remove outright.
DEEP_MAKE_ROOTS=()
DEEP_MAKE_DRYRUNS=()
DEEP_CARGO_TARGETS=()
DEEP_GO_ROOTS=()
DEEP_REMOVALS=()

ok() {
    printf 'ok:   %s\n' "$1"
}

fail() {
    GC_FAILED=1
    printf 'FAIL: %s\n' "$1" >&2
    shift
    local remedy
    for remedy in "$@"; do
        printf '      %s\n' "${remedy}" >&2
    done
}

step() {
    local label="$1" rc
    shift
    if "$@"; then
        ok "${label}"
    else
        rc=$?
        fail "${label} (exit ${rc})"
    fi
}

# MEASURE_AS is set by the tier function below before this is ever called:
# "sudo" in the machine tier, empty in the user tier.
MEASURE_AS=

measure() {
    # -x, because a bind mount of the same device would otherwise be counted
    # twice. No stderr suppression here: a `du` that cannot read part of the
    # tree still prints whatever total it could gather, and the permission
    # error itself is left to flow back over ssh rather than being silenced.
    local size
    if [ -n "${MEASURE_AS}" ]; then
        # sudo also for the existence test: /root is mode 700, so an
        # unprivileged -e on /root/.cache/nix says "absent" about a path
        # that is really there.
        sudo test -e "$1" || return 0
        size="$(sudo du -x -s -h "$1" | awk '{print $1}')" || true
    else
        test -e "$1" || return 0
        size="$(du -x -s -h "$1" | awk '{print $1}')" || true
    fi
    printf '  %-7s %s%s\n' "${size:-?}" "$1" "${2:+  ($2)}"
}

# The ESP is small and GRUB copies the incoming kernel before pruning old
# ones, so a nixos-rebuild that runs out of space there
# advances the system profile and then dies installing the bootloader,
# leaving the booted generation and the profile pointing at different
# closures. gc-machine's half-failed-switch guard reads this pair. `yo
# bootstrap` reads the same two paths over plain ssh (guest_generations), because
# a fresh nixos-lima image has no yolobox-guest yet; keep the two in step.
generations() {
    local profile="" booted=""
    profile="$(readlink -f /nix/var/nix/profiles/system)" || true
    booted="$(readlink -f /run/current-system)" || true
    printf 'profile=%s\n' "${profile}"
    printf 'booted=%s\n' "${booted}"
}

# Sets TARGETS_FILE to a temp file holding the walk for every directory gc
# --deep may act on, so the caller can read it with a plain `while read` loop.
# A process substitution would hide the walk's exit status; returning the path
# through a global rather than stdout is what keeps `fail` effective, because
# a command substitution would run this in a subshell and discard GC_FAILED
# with it.
build_targets() {
    build_expr "${BUILD_DIRS[@]}" "${REMOVED_OUTRIGHT[@]}"
    local rc=0
    TARGETS_FILE="$(mktemp)"
    find "$HOME" -mindepth 1 \
        "${EXPR[@]}" -type d -prune -print0 -o \
        -name '.*' -prune >"${TARGETS_FILE}" || rc=$?
    if [ "${rc}" != 0 ]; then
        fail "the build-directory walk under \$HOME could not read every directory (find exit ${rc})" \
            "A directory it could not open is invisible to --deep; nothing under it is" \
            "measured or deleted."
    fi
}

# Fills CACHE_PATHS with every existing cache directory gc --yes reclaims: the
# LOCAL_CACHES, cargo's downloaded sources, and the Go caches. A project or a
# shell rc file can point GOPATH anywhere, so Go's caches are found by
# signature, not by path: a module cache is a `pkg/mod` holding
# `cache/download`, a build cache is a directory whose README opens with
# GO_BUILD_CACHE_HEADER. The walk stops at depth 5: that reaches a GOPATH
# three directories below $HOME (its pkg/mod sits at depth 5) and a build
# cache four down (its README sits at depth 5); the default ~/go is one down.
# Hidden directories are pruned, which also keeps ~/.cache/go-build out of it
# (LOCAL_CACHES already names it).
# A global rather than stdout, for the same reason as TARGETS_FILE.
CACHE_PATHS=()

add_cache() {
    if [ -e "$1" ]; then
        CACHE_PATHS+=("$1")
    fi
}

# A cache the walk found by signature may sit inside a project checkout, where
# git may track or see it (a vendored module cache). Then it is the project's,
# not a cache: deleting it would dirty the worktree, so it is kept and failed
# like a build directory. The fixed names under ~/.cache and ~/.cargo need no
# such gate; they are the tools' own.
add_found_cache() {
    if git -C "${1%/*}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        && ! git_ignores "$1"; then
        fail_not_ignored "$1"
        return 0
    fi
    add_cache "$1"
}

cache_targets() {
    CACHE_PATHS=()
    local name path
    for name in "${LOCAL_CACHES[@]}"; do
        add_cache "$HOME/.cache/${name}"
    done
    add_cache "$HOME/.cargo/registry"
    add_cache "$HOME/.cargo/git"

    build_expr "${BUILD_DIRS[@]}"
    local candidates rc=0
    candidates="$(mktemp)"
    find "$HOME" -mindepth 1 -maxdepth 5 \
        "${EXPR[@]}" -type d -prune -o \
        -name '.*' -prune -o \
        -type d -path '*/pkg/mod' -print0 -prune -o \
        -type f -name README -printf '%h\0' >"${candidates}" || rc=$?
    if [ "${rc}" != 0 ]; then
        fail "the Go cache walk under \$HOME could not read every directory (find exit ${rc})" \
            "A Go cache under a directory it could not open is invisible to gc."
    fi
    while IFS= read -r -d '' path; do
        case "${path}" in
        */pkg/mod)
            if [ -d "${path}/cache/download" ]; then
                add_found_cache "${path}"
            fi
            ;;
        *)
            # $HOME itself is never a cache: a README at the top of the home
            # would otherwise nominate the whole home for deletion. Go also
            # writes trim.txt beside the README, so a stray README that merely
            # starts with the same line is not enough.
            if [ "${path}" != "${HOME}" ] \
                && [ -f "${path}/trim.txt" ] \
                && [ "$(head -n 1 "${path}/README")" = "${GO_BUILD_CACHE_HEADER}" ]; then
                add_found_cache "${path}"
            fi
            ;;
        esac
    done <"${candidates}"
    rm -f "${candidates}"
}

# Go's module cache is read-only (directories 0555, files 0444), so a bare
# rm -rf fails on it. A symlink is removed as the link: chmod -R through it
# would rewrite permissions on whatever it points at.
remove_tree() {
    if [ -L "$1" ]; then
        rm -f -- "$1"
    else
        chmod -R u+w -- "$1" && rm -rf -- "$1"
    fi
}

# Runs CMD with cwd ROOT, in the environment the project itself declares:
# through devbox when ROOT/devbox.json exists, else directly. cargo, go and
# make are on no box-wide PATH; each project's environment provides them.
# devbox joins its arguments into one script string that its own bash runs,
# so a `$`, a quote or a backtick in an argument is expanded or broken there,
# not passed on; every caller passes plain words. -q drops devbox's own
# `Info:` lines while its `Error:` lines and the command's stderr still show.
# stdin is /dev/null: the callers sit inside `while read` loops over a file,
# and a tool that reads stdin would otherwise eat the rest of the list.
in_project_env() {
    local root="$1"
    shift
    if [ -f "${root}/devbox.json" ]; then
        (cd "${root}" && devbox run -q -- "$@") </dev/null
    else
        (cd "${root}" && "$@") </dev/null
    fi
}

# Succeeds when TOOL resolves in ROOT's environment, otherwise reports why the
# tool will not be run. TOOL is one of the fixed words make, cargo and go, so
# spelling it into the script string is safe (devbox drops positional
# parameters after `sh -c`, see in_project_env).
require_tool() {
    local root="$1" tool="$2" what="$3"
    if in_project_env "${root}" sh -c "command -v ${tool} >/dev/null"; then
        return 0
    fi
    fail "kept ${what}: ${tool} is not in ${root}'s environment" \
        "Add ${tool} to the project's devbox.json, or clean it by hand."
    return 1
}

# The manifest NAME needs beside it to count as build output, from
# NEEDS_MANIFEST; empty for a name that needs none.
manifest_for() {
    local entry
    for entry in "${NEEDS_MANIFEST[@]}"; do
        if [ "${entry%%:*}" = "$1" ]; then
            printf '%s' "${entry#*:}"
        fi
    done
}

# Adds ROOT to the make-clean actions when its Makefile has a clean target.
# `make -n clean` decides: exit 0 means there is one; make's own "No rule to
# make target 'clean'" means there is none, which is no business of ours (GNU
# make quotes with apostrophes, older versions with a backtick and an
# apostrophe); anything else, "No rule to make target 'x', needed by 'clean'"
# included, is a broken Makefile and is reported with make's output.
plan_make() {
    local root="$1" out rc=0
    if ! require_tool "${root}" make "make clean in ${root}"; then
        return 0
    fi
    out="$(in_project_env "${root}" make -n clean 2>&1)" || rc=$?
    if [ "${rc}" = 0 ]; then
        DEEP_MAKE_ROOTS+=("${root}")
        DEEP_MAKE_DRYRUNS+=("${out}")
        return 0
    fi
    case "${out}" in
    *"No rule to make target 'clean'"* | *"No rule to make target \`clean'"*) ;;
    *) fail "make -n clean failed in ${root} (exit ${rc})" "${out}" ;;
    esac
}

# Decides what gc --deep would do, filling the DEEP_* globals, and prints a
# line for every directory it leaves alone for want of its manifest. Nothing
# is deleted or cleaned here; the only commands run are the tool lookups and
# `make -n clean`.
deep_plan() {
    DEEP_MAKE_ROOTS=()
    DEEP_MAKE_DRYRUNS=()
    DEEP_CARGO_TARGETS=()
    DEEP_GO_ROOTS=()
    DEEP_REMOVALS=()

    local roots_file rc=0 root
    roots_file="$(mktemp)"
    project_roots >"${roots_file}" || rc=$?
    if [ "${rc}" != 0 ]; then
        fail "the project walk under \$HOME could not read every directory (find exit ${rc})" \
            "A project under a directory it could not open is invisible to --deep;" \
            "no make clean or go clean runs for it."
    fi
    while IFS= read -r -d '' root; do
        if [ -f "${root}/Makefile" ] || [ -f "${root}/GNUmakefile" ] || [ -f "${root}/makefile" ]; then
            plan_make "${root}"
        fi
        if [ -f "${root}/go.mod" ] && require_tool "${root}" go "go clean in ${root}"; then
            DEEP_GO_ROOTS+=("${root}")
        fi
    done <"${roots_file}"
    rm -f "${roots_file}"

    local dir name parent manifest
    build_targets
    while IFS= read -r -d '' dir; do
        name="${dir##*/}"
        parent="${dir%/*}"
        # Outside every repo there is no project to own it and no .gitignore
        # to vouch for it.
        if ! git -C "${parent}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            printf 'kept %s: not inside a git repository\n' "${dir}"
            continue
        fi
        manifest="$(manifest_for "${name}")"
        if [ -n "${manifest}" ] && [ ! -f "${parent}/${manifest}" ]; then
            case "${name}" in
            target) printf "kept %s: no %s beside it, so it is not cargo's build output\n" "${dir}" "${manifest}" ;;
            *) printf 'kept %s: no %s beside it\n' "${dir}" "${manifest}" ;;
            esac
            continue
        fi
        # A name build_targets learns must be given its action here; without
        # one the tier stops loudly instead of removing it by default.
        case "${name}" in
        target)
            if require_tool "${parent}" cargo "${dir}"; then
                DEEP_CARGO_TARGETS+=("${dir}")
            fi
            ;;
        node_modules | .venv | .next) DEEP_REMOVALS+=("${dir}") ;;
        *) fail "gc --deep has no action for ${dir}" "Give '${name}' a case in deep_plan." ;;
        esac
    done <"${TARGETS_FILE}"
    rm -f "${TARGETS_FILE}"
}

# Deleting or cleaning a path git does not ignore dirties the VM worktree:
# tracked files vanish from it, to be noticed much later. The name alone does
# not settle it: an anchored /node_modules in .gitignore leaves a nested one
# tracked. A directory that ignores itself (uv and venv write a `*` .gitignore
# into a .venv) is not reported ignored, but git neither tracks nor sees
# anything in it, which is the same promise.
git_ignores() {
    local parent="${1%/*}" seen
    if git -C "${parent}" check-ignore -q "$1"; then
        return 0
    fi
    seen="$(git -C "${parent}" ls-files -- "$1" \
        && git -C "${parent}" status --porcelain --untracked-files=all -- "$1")" || return 1
    [ -z "${seen}" ]
}

# The remedy depends on why git does not ignore it: a .gitignore line does
# nothing for files git already tracks.
fail_not_ignored() {
    local tracked
    tracked="$(git -C "${1%/*}" ls-files -- "$1")" || tracked=
    if [ -n "${tracked}" ]; then
        fail "kept $1: git tracks files in it" \
            "Deleting it would remove tracked files from the worktree. If it" \
            "really is build output, \`git rm -r --cached\` it, add it to that" \
            "repo's .gitignore and commit."
    else
        fail "kept $1: git does not ignore it" \
            "Deleting it would dirty the worktree. Add it to that repo's" \
            ".gitignore (or remove it by hand and commit) if it really is" \
            "build output."
    fi
}

report_deep_plan() {
    local i dir root found=0
    for ((i = 0; i < ${#DEEP_MAKE_ROOTS[@]}; i++)); do
        found=1
        printf '%s: make clean would run:\n' "${DEEP_MAKE_ROOTS[i]}"
        printf '%s\n' "${DEEP_MAKE_DRYRUNS[i]}" | sed 's/^/    /'
    done
    for dir in "${DEEP_CARGO_TARGETS[@]}"; do
        found=1
        measure "${dir}" 'cargo clean'
    done
    for root in "${DEEP_GO_ROOTS[@]}"; do
        found=1
        printf '%s: go clean\n' "${root}"
    done
    for dir in "${DEEP_REMOVALS[@]}"; do
        found=1
        measure "${dir}" remove
    done
    [ "${found}" = 1 ] || printf '  (none)\n'
}

# Runs a step on DIR when git ignores DIR, fails it when git does not, and
# does nothing when DIR is already gone: an earlier clean (a Makefile's
# `rm -rf target`) can remove it after the plan was made, and git check-ignore
# on a missing directory says "not ignored" for a dir-only pattern like
# `target/`.
step_if_ignored() {
    local dir="$1"
    shift
    if [ ! -e "${dir}" ]; then
        ok "already gone: ${dir}"
    elif git_ignores "${dir}"; then
        step "$@"
    else
        fail_not_ignored "${dir}"
    fi
}

# Runs the plan: make clean, then cargo clean, then go clean, then the
# outright removals, every one through `step`. A target or removal that git
# does not ignore is kept and failed; make clean and go clean are the
# project's own definition of clean and are not guarded.
apply_deep_plan() {
    local root dir
    for root in "${DEEP_MAKE_ROOTS[@]}"; do
        step "make clean in ${root}" in_project_env "${root}" make clean
    done
    for dir in "${DEEP_CARGO_TARGETS[@]}"; do
        step_if_ignored "${dir}" "cargo clean in ${dir%/*}" in_project_env "${dir%/*}" cargo clean
    done
    for root in "${DEEP_GO_ROOTS[@]}"; do
        step "go clean in ${root}" in_project_env "${root}" go clean
    done
    for dir in "${DEEP_REMOVALS[@]}"; do
        step_if_ignored "${dir}" "rm -rf ${dir}" remove_tree "${dir}"
    done
}

# Claude's scratchpads are live session output, not ours to delete. Reported
# so a full disk can be traced to them.
report_scratchpads() {
    printf '\n/tmp scratchpads (reported only, never deleted; systemd ages /tmp after 10 days idle):\n'
    measure "/tmp/claude-$(id -u)"
}

# Every git repo under $HOME, NUL-separated. `projects` prints it as is; gc
# --deep reads it to find the roots whose own tools clean them.
project_roots() {
    build_expr "${BUILD_DIRS[@]}"
    find "$HOME" -mindepth 1 \
        "${EXPR[@]}" -prune -o \
        -name .git \( -type d -o -type f \) -printf '%h\0' -prune -o \
        -name '.*' -prune
}

cmd_projects() {
    want_args projects 'projects (takes no arguments)' 0 "$@"
    project_roots
}

# Prints the deepest existing ancestor of an absolute path, PATH itself when
# it exists, and `/` when nothing under it does: yo decides what to make of
# a landing place outside the home, so a wrong answer must not be the home.
cmd_landing_dir() {
    want_args landing-dir 'landing-dir PATH' 1 "$@"
    local d="$1"
    case "${d}" in
    /*) ;;
    *)
        printf 'yolobox-guest: landing-dir: path must be absolute: %s\n' "${d}" >&2
        exit 2
        ;;
    esac
    while [ -n "${d}" ] && [ ! -d "${d}" ]; do d="${d%/*}"; done
    printf '%s\n' "${d:-/}"
}

cmd_gc_machine() {
    local apply=0 arg
    for arg in "$@"; do
        case "${arg}" in
        --apply) apply=1 ;;
        *)
            printf "yolobox-guest: gc-machine: unknown option '%s'\n" "${arg}" >&2
            exit 2
            ;;
        esac
    done

    # Every measure and step below runs under sudo. A sudo that cannot run
    # (a caller outside wheel, a password prompt with no tty) must stop here:
    # measure would otherwise read its failure as "absent" for every path.
    if ! sudo -n true; then
        printf 'yolobox-guest: gc-machine: sudo -n true failed for %s; the machine tier needs sudo\n' "$(id -un)" >&2
        exit 1
    fi

    GC_FAILED=0
    MEASURE_AS=sudo
    # Nix's download caches: root's, and the operator's own, which every
    # `nixos-rebuild --flake` run as the operator fills (mostly gitv3).
    local nix_caches=(/root/.cache/nix "${HOME}/.cache/nix")
    local machine_paths=(/var/log/journal /nix/store "${nix_caches[@]}" /var/lib/systemd/coredump)

    if [ "${apply}" != 1 ]; then
        printf 'yo gc: report only, nothing is deleted. Add --yes to act.\n\n'
        df -h /
        printf '\nmachine tier (current size of each target):\n'
        local path
        for path in "${machine_paths[@]}"; do
            measure "${path}"
        done
        exit 0
    fi

    printf 'before:\n'
    df -h /
    printf '\n'

    # Root-only steps first, and that ordering is the point: ext4's reserve
    # is root's alone, so on a full disk the agent's unprivileged steps
    # cannot even write their own logs until these have freed something.
    # --rotate before --vacuum-size because vacuuming only ever unlinks
    # archived journals; without the rotation the active file — usually most
    # of the journal — is untouchable.
    step 'journalctl --rotate --vacuum-size=50M' sudo journalctl --rotate --vacuum-size=50M

    # `-d` deletes every generation but the profile's, which in the
    # half-failed-switch state is the one that is *not* running — the box
    # would lose its own booted system.
    local gen_out profile_line booted_line profile booted
    gen_out="$(generations)"
    profile_line="${gen_out%%$'\n'*}"
    booted_line="${gen_out#*$'\n'}"
    profile="${profile_line#profile=}"
    booted="${booted_line#booted=}"
    if [ -n "${booted}" ] && [ "${booted}" = "${profile}" ]; then
        step 'nix-collect-garbage -d' sudo nix-collect-garbage -d
        # Hard-links identical store files; what auto-optimise-store does for
        # new paths, this does for the ones already there.
        step 'nix-store --optimise' sudo nix-store --optimise
    else
        fail "skipped nix-collect-garbage -d: booted and profile generations disagree" \
            "booted:  ${booted:-<unreadable>}" \
            "profile: ${profile:-<unreadable>}" \
            "A nixos-rebuild switch half-failed (check df /boot). Re-run the switch" \
            "until both agree, then ./yo gc --yes again."
    fi

    # Outside that guard on purpose: these are nix's download caches and the
    # coredump store, tied to no generation, so they are safe to drop even
    # when the profile is in the broken state above — and that is exactly
    # when the space is most needed.
    local c
    for c in "${nix_caches[@]}"; do
        step "rm -rf ${c}" sudo rm -rf "${c}"
    done
    # Crash dumps are diagnostics nobody reads after the fact; `coredumpctl`
    # lists them before --yes if you need one. Contents only: systemd owns
    # the directory.
    step 'empty /var/lib/systemd/coredump' sudo find /var/lib/systemd/coredump -mindepth 1 -delete

    [ "${GC_FAILED}" = 0 ] || exit 1
}

# Rootless podman keeps its files under subuids the agent cannot read, so `du`
# undercounts them and floods stderr; podman's own accounting is the right one.
report_podman() {
    local out
    printf '\npodman storage (podman system df; --deep prunes unused images, no tier ever prunes volumes):\n'
    if out="$(podman system df 2>&1)"; then
        printf '%s\n' "${out}" | sed 's/^/  /'
    else
        fail "podman system df" "${out}"
    fi
}

cmd_gc_user() {
    local apply=0 deep=0 arg
    for arg in "$@"; do
        case "${arg}" in
        --apply) apply=1 ;;
        --deep) deep=1 ;;
        *)
            printf "yolobox-guest: gc-user: unknown option '%s'\n" "${arg}" >&2
            exit 2
            ;;
        esac
    done

    GC_FAILED=0
    MEASURE_AS=
    local user_paths=("$HOME/.npm")

    local path
    cache_targets

    if [ "${apply}" != 1 ]; then
        printf 'user tier (current size of each target):\n'
        for path in "${user_paths[@]}"; do
            measure "${path}"
        done
        report_podman
        printf '\ncache tier (rebuilt or refetched on demand; --yes reclaims it):\n'
        for path in "${CACHE_PATHS[@]}"; do
            measure "${path}"
        done
        report_scratchpads
        if [ "${deep}" = 1 ]; then
            printf '\nproject tier (--deep):\n'
            deep_plan
            report_deep_plan
        else
            printf '\nproject tier not measured; add --deep to include it.\n'
        fi
        [ "${GC_FAILED}" = 0 ] || exit 1
        exit 0
    fi

    step 'npm cache clean --force' npm cache clean --force
    # -f only: stopped containers, unused networks and dangling images and
    # build cache; volumes are kept. Images still tagged but unused need a
    # registry pull to come back, which is not regenerable offline, so they
    # wait for --deep. nix/podman.nix already runs a periodic prune besides.
    step 'podman system prune -f' podman system prune -f
    for path in "${CACHE_PATHS[@]}"; do
        step "rm -rf ${path}" remove_tree "${path}"
    done
    report_scratchpads

    if [ "${deep}" = 1 ]; then
        step 'podman system prune -af' podman system prune -af
        deep_plan
        apply_deep_plan
    fi

    printf '\nafter:\n'
    df -h /

    [ "${GC_FAILED}" = 0 ] || exit 1
}

cmd_aws_creds() {
    want_args aws-creds 'aws-creds PROFILE' 1 "$@"
    local profile="$1"

    # The same shape yo's allowlist enforces; checked here too because the
    # name goes into a URL path and this is the process that builds it.
    if ! [[ "${profile}" =~ ^${YOLOBOX_PROFILE_NAME_RE}$ ]]; then
        printf 'yolobox-guest: aws-creds: invalid profile name: %s\n' "${profile}" >&2
        exit 2
    fi

    # stdout and stderr are kept apart: process-credential JSON is read from
    # stdout, so curl's own progress or warnings must never join it.
    local response errors errfile rc=0
    errfile="$(mktemp)"
    response="$(curl -sS -g --fail-with-body --unix-socket "${YOLOBOX_AGENT_AWS_BROKER_SOCK}" "http://broker/creds/${profile}" 2>"${errfile}")" || rc=$?
    errors="$(cat "${errfile}")"
    rm -f "${errfile}"

    if [ "${rc}" = 0 ]; then
        printf '%s\n' "${response}"
        return 0
    fi

    # Once yo has started the socket unit, systemd accepts a connection on
    # it even with no proxy or broker behind it, so a dead forward shows up as
    # curl 52/56 (empty reply / connection reset). 7 (couldn't connect) is a
    # socket unit nothing has started since boot.
    case "${rc}" in
    7 | 52 | 56)
        # shellcheck disable=SC2016
        printf 'yolobox-guest: AWS broker socket %s not answering; run `yo up` on the host\n' "${YOLOBOX_AGENT_AWS_BROKER_SOCK}" >&2
        exit 1
        ;;
    esac

    printf '%s\n%s\n' "${errors}" "${response}" >&2
    exit 1
}

cmd_aws_check() {
    want_args aws-check 'aws-check (takes no arguments)' 0 "$@"
    local config_file="${AWS_CONFIG_FILE:-}"

    if [ -z "${config_file}" ]; then
        printf 'yolobox-guest: AWS_CONFIG_FILE is not set\n' >&2
        exit 1
    fi

    if [ ! -f "${config_file}" ]; then
        printf 'yolobox-guest: AWS_CONFIG_FILE does not exist: %s\n' "${config_file}" >&2
        exit 1
    fi

    local failed=0 profile profiles
    # A plain capture, not a process substitution, so a failing awk stops
    # here; and a file with no profile is a failed check, not a vacuous pass.
    profiles="$(awk '/^\[profile / {
        s = $0;
        sub(/^\[profile /, "", s);
        sub(/\].*/, "", s);
        print s;
    }
    /^\[default\]/ {
        print "default";
    }' "${config_file}")"
    if [ -z "${profiles}" ]; then
        printf 'yolobox-guest: no profile sections in %s\n' "${config_file}" >&2
        exit 1
    fi

    while IFS= read -r profile; do
        local arn error_msg rc=0
        arn="$(AWS_PROFILE="${profile}" aws sts get-caller-identity --query Arn --output text 2>&1 </dev/null)" || rc=$?

        if [ "${rc}" = 0 ]; then
            printf 'OK %s %s\n' "${profile}" "${arn}"
        else
            error_msg="$(printf '%s\n' "${arn}" | head -1)"
            printf 'FAIL %s %s\n' "${profile}" "${error_msg}"
            failed=1
        fi
    done <<<"${profiles}"

    [ "${failed}" = 0 ] || exit 1
}

# The one egress probe. It prints the word status reports: ok when curl
# reached the address and got an HTTP status back (its %{http_code}), dead when
# curl says it could not connect (exit 7) or timed out (exit 28), and any other
# answer as `unknown:` with curl's own words, so a missing curl, a TLS failure
# or a code it never prints is shown, never read as either. An address, not a
# name: the box resolves through the gateway, so a probe by name dies with the
# gateway's DNS and egress could never be told apart from it.
EGRESS_PROBE_URL=https://1.1.1.1
EGRESS_PROBE_SECONDS=5

probe_egress() {
    local out rc=0
    out=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${EGRESS_PROBE_SECONDS}" "${EGRESS_PROBE_URL}" 2>&1) || rc=$?
    if [ "${rc}" = 0 ] && [[ ${out} =~ ^[1-5][0-9][0-9]$ ]]; then
        echo ok
    elif [ "${rc}" = 7 ] || [ "${rc}" = 28 ]; then
        echo dead
    else
        echo "unknown: curl exited ${rc}: ${out//$'\n'/ }"
    fi
}

# The gateway's DNS probe, printing the word status reports. ok when the
# gateway resolved a real name to an address. Lima's gateway answers some
# questions itself while a name it must ask the host for can time out, so only
# a resolved address proves lookups work. dead when the gateway answered with
# no address (nothing, or a CNAME chain alone) or dig says no reply came; any
# other answer is `unknown:` with dig's words. dig's exit code says nothing:
# bind9 reports 0 on an ICMP unreachable or a reset connection
# (isc-projects/bind9 #3235, #3615), and +short prints its errors as ;; lines
# on stdout.
GATEWAY_DNS_PROBE_NAME=github.com

# Whether a line of dig's is its word for a question no server answered: `;;
# no servers could be reached`, or bind 9.18's `;; communications error to
# ADDR#53:` and `timed out`, `connection refused` or `host unreachable` (the
# egress table's ICMP answer). The case differs across bind versions, so it is
# matched without it.
dig_no_reply() {
    local found=1
    shopt -s nocasematch
    case "$1" in
    ';;'*'timed out'* | ';;'*'no servers could be reached'* | ';;'*'connection refused'* | ';;'*'unreachable'*)
        found=0
        ;;
    esac
    shopt -u nocasematch
    return "${found}"
}

probe_gateway_dns() {
    local reply line rc=0 address=0 unnamed=0
    reply=$(dig +short +time=2 +tries=1 "@${YOLOBOX_GATEWAY}" "${GATEWAY_DNS_PROBE_NAME}" A 2>&1) || rc=$?
    while IFS= read -r line; do
        if [[ ${line} =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            address=1
        elif [ -z "${line}" ] || [[ ${line} =~ ^([A-Za-z0-9-]+\.)+$ ]]; then
            :
        elif dig_no_reply "${line}"; then
            :
        else
            unnamed=1
        fi
    done <<<"${reply}"
    if [ "${address}" = 1 ]; then
        echo ok
    elif [ "${unnamed}" = 0 ]; then
        echo dead
    else
        echo "unknown: dig exited ${rc}: ${reply//$'\n'/ }"
    fi
}

# Free space on / below which status reports the disk full. `stat -f` counts
# the blocks ext4 reserves for root (%f), which `df`'s Avail column leaves out:
# dhcpcd runs as root, and only when those are gone does a full
# disk look like a DNS failure (empty resolv.conf, ENOSPC in dhcpcd).
ROOT_MIN_FREE_MIB=64

# Prints the whole MiB free on / for root. A stat that fails or prints anything
# but two numbers returns 1 with the reason on stderr, never "not full".
root_free_mib() {
    local blocks free_blocks block_bytes
    blocks=$(stat -f -c '%f %S' /) || return 1
    read -r free_blocks block_bytes <<<"${blocks}"
    case "${free_blocks}${block_bytes}" in
    '' | *[!0-9]*)
        printf 'yolobox-guest: stat -f / answered %q, not two numbers\n' "${blocks}" >&2
        return 1
        ;;
    esac
    printf '%s\n' "$((free_blocks * block_bytes / 1024 / 1024))"
}

# Use% of / at which status reports the root filesystem nearly full.
ROOTFS_FULL_PERCENT=90

# The socket units that hand the agent the host's forwarded sockets, judged
# like the egress unit by their ActiveState.
PROXY_UNITS=("${YOLOBOX_OP_AGENT_PROXY_UNIT}.socket" "${YOLOBOX_AWS_BROKER_PROXY_UNIT}.socket")

# One KEY=value line of the status block. A value is cut at its first newline,
# so a probe that prints several lines cannot forge a second key, and an empty
# one is a key yo reports as not reported.
field() {
    printf '%s=%s\n' "$1" "${2%%$'\n'*}"
}

# A unit's ActiveState; empty when systemctl cannot say.
unit_state() {
    systemctl show -p ActiveState --value "$1" 2>/dev/null || true
}

# `df -h /`'s data line, as the operator reads it; empty when df fails.
rootfs_line() {
    local out
    out=$(df -h / 2>/dev/null) || return 0
    sed -n 2p <<<"${out}"
}

# yes when / is ROOTFS_FULL_PERCENT or more in use (df -P's Use% column), no
# otherwise, empty when df answers no number.
rootfs_full() {
    local out pct
    out=$(df -P / 2>/dev/null) || return 0
    pct=$(awk 'NR == 2 { sub("%", "", $5); print $5 }' <<<"${out}")
    case "${pct}" in
    '' | *[!0-9]*) return 0 ;;
    esac
    if [ "${pct}" -ge "${ROOTFS_FULL_PERCENT}" ]; then
        printf 'yes\n'
    else
        printf 'no\n'
    fi
}

# Everything `yo status` asks the box, as KEY=value lines in one round trip;
# yo prints a line per key and warns about a key that is missing, so a probe
# that fails leaves its value empty and never fails the block. internet= and
# gateway_dns= tell apart the two host-VPN shapes: a VPN that owns the host's
# default route kills egress while the gateway still answers DNS; one that
# blackholes lima's gateway leaves egress working while the gateway's DNS goes
# silent. rootdisk=full names a root disk too full for dhcpcd to write
# resolv.conf, which would otherwise be blamed on the network.
cmd_status() {
    want_args status 'status (takes no arguments)' 0 "$@"
    local unit free_mib rootdisk=''
    field version "$(cat "${YOLOBOX_VERSION_STAMP}" 2>/dev/null || true)"
    field flake_ref "$(cat "${YOLOBOX_FLAKE_REF_STAMP}" 2>/dev/null || true)"
    field egress "$(unit_state "${YOLOBOX_EGRESS_UNIT}")"
    field network_ready "$(unit_state "${YOLOBOX_NETWORK_READY_UNIT}")"
    for unit in "${PROXY_UNITS[@]}"; do
        field "${unit}" "$(unit_state "${unit}")"
    done
    field fstype "$(lsblk -dno FSTYPE "${YOLOBOX_LUKS_DEVICE}" 2>/dev/null || true)"
    field rootfs "$(rootfs_line)"
    field rootfs_full "$(rootfs_full)"
    if free_mib=$(root_free_mib); then
        rootdisk=ok
        if [ "${free_mib}" -lt "${ROOT_MIN_FREE_MIB}" ]; then
            rootdisk=full
        fi
    fi
    field rootdisk "${rootdisk}"
    field internet "$(probe_egress)"
    field gateway_dns "$(probe_gateway_dns)"
}

# Prints the one line of the flake-ref stamp. Fails, naming the file, when it
# is missing, empty or holds more than one line: nix/base.nix writes one value
# and a newline, so anything else is not a stamp.
read_stamp() {
    local file=$1 line value="" count=0
    if [ ! -f "${file}" ]; then
        printf 'yolobox-guest: rebuild: %s is missing\n' "${file}" >&2
        return 1
    fi
    # `|| [ -n ... ]` keeps a last line that has no newline.
    while IFS= read -r line || [ -n "${line}" ]; do
        count=$((count + 1))
        value=${line}
    done <"${file}"
    if [ "${count}" -ne 1 ] || [ -z "${value}" ]; then
        printf 'yolobox-guest: rebuild: %s must hold exactly one non-empty line\n' "${file}" >&2
        return 1
    fi
    printf '%s\n' "${value}"
}

# The one way to rebuild the box from inside it: the flake ref is the exact
# tree yo bootstrap built (github:OWNER/REPO/<rev>?narHash=<hash>, stamped by
# nix/base.nix), and the hostname is the kernel's, so a hand rebuild can
# neither switch trees nor rename an -m machine. A different ref goes through
# `yo bootstrap --version` on the host. Only switch and boot, because they are
# the actions that keep the box what yo built. The flags are yo's
# NIX_REBUILD_FLAGS.
cmd_rebuild() {
    want_args rebuild 'rebuild switch|boot' 1 "$@"
    if [ "$1" != switch ] && [ "$1" != boot ]; then
        printf 'yolobox-guest: rebuild: usage: rebuild switch|boot\n' >&2
        exit 2
    fi
    local action=$1 uid user ref host
    uid=$(id -u)
    if [ "${uid}" != "${YOLOBOX_OPERATOR_UID}" ]; then
        printf 'yolobox-guest: rebuild: run as the operator (uid %s), not uid %s\n' \
            "${YOLOBOX_OPERATOR_UID}" "${uid}" >&2
        exit 2
    fi
    ref=$(read_stamp "${YOLOBOX_FLAKE_REF_STAMP}") || exit 1
    case "${ref}" in
    github:*) ;;
    *)
        printf 'yolobox-guest: rebuild: %s holds %q, not a github: flake ref\n' "${YOLOBOX_FLAKE_REF_STAMP}" "${ref}" >&2
        exit 1
        ;;
    esac
    host=$(uname -n)
    user=$(id -un)
    local status=0
    sudo env "${YOLOBOX_NIX_ENV_USERNAME}=${user}" "${YOLOBOX_NIX_ENV_HOSTNAME}=${host}" \
        "${YOLOBOX_NIX_ENV_FLAKE_REF}=${ref}" \
        flock -n -E "${YOLOBOX_REBUILD_BUSY_EXIT}" "${YOLOBOX_REBUILD_LOCK}" \
        nixos-rebuild "${action}" --impure --flake "${ref}#yolobox" || status=$?
    if [ "${status}" -eq "${YOLOBOX_REBUILD_BUSY_EXIT}" ]; then
        printf 'yolobox-guest: rebuild: another rebuild is running (it holds %s); wait for it to finish\n' \
            "${YOLOBOX_REBUILD_LOCK}" >&2
    fi
    exit "${status}"
}

main() {
    local sub="${1:-}"
    [ "$#" -ge 1 ] && shift
    case "${sub}" in
    projects) cmd_projects "$@" ;;
    landing-dir) cmd_landing_dir "$@" ;;
    gc-machine) cmd_gc_machine "$@" ;;
    gc-user) cmd_gc_user "$@" ;;
    aws-creds) cmd_aws_creds "$@" ;;
    aws-check) cmd_aws_check "$@" ;;
    status) cmd_status "$@" ;;
    rebuild) cmd_rebuild "$@" ;;
    *)
        printf "yolobox-guest: unknown subcommand '%s'\n" "${sub}" >&2
        exit "${YOLOBOX_USAGE_EXIT}"
        ;;
    esac
}

main "$@"
