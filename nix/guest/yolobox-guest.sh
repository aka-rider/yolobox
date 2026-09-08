#!/usr/bin/env bash
# The guest-side half of `yo`: subcommands over ssh, one per account, so the
# find expressions, the gc tiers and the AWS probe are shellchecked at build
# time instead of living in yo's Python string literals where nothing checks
# them. Subcommands: projects, home-roots, landing-dir, ensure-repo,
# generations, gc-machine, gc-user, aws-check.
#
# writeShellApplication prepends `set -o errexit -o nounset -o pipefail`. The
# gc tiers below were written for a shell with none of that — a cleanup step
# that legitimately finds nothing to do still exits nonzero (podman does),
# and that must not cancel the steps after it. `step` stays safe as-is,
# because errexit does not fire on a command that is the condition of an
# `if`; the few captures that are NOT already inside an `if` are guarded
# individually with `|| true` or an explicit `rc=$?`, right where they are.

# The one place build-output directory names are spelled. The project walk
# prunes them (a repo inside node_modules is not a project); gc --deep
# matches them (they are the thing being deleted).
BUILD_DIRS=(node_modules target .next)

build_expr() {
    EXPR=('(')
    local name
    for name in "${BUILD_DIRS[@]}"; do
        [ "${#EXPR[@]}" -gt 1 ] && EXPR+=(-o)
        EXPR+=(-name "${name}")
    done
    EXPR+=(')')
}

GC_FAILED=0
TARGETS_FILE=

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
    printf '  %-7s %s\n' "${size:-?}" "$1"
}

# The ESP is small and GRUB copies the incoming kernel before pruning old
# ones (see CLAUDE.md), so a nixos-rebuild that runs out of space there
# advances the system profile and then dies installing the bootloader,
# leaving the booted generation and the profile pointing at different
# closures. Both `generations` (the subcommand) and gc-machine's
# half-failed-switch guard read this same pair, so the two can never
# disagree on which path is which.
generations() {
    local profile="" booted=""
    profile="$(readlink -f /nix/var/nix/profiles/system)" || true
    booted="$(readlink -f /run/current-system)" || true
    printf 'profile=%s\n' "${profile}"
    printf 'booted=%s\n' "${booted}"
}

# Sets TARGETS_FILE to a temp file holding the build-directory walk, so the
# caller can read it with a plain `while read` loop. A process substitution
# would hide the walk's exit status; returning the path through a global
# rather than stdout is what keeps `fail` effective, because a command
# substitution would run this in a subshell and discard GC_FAILED with it.
build_targets() {
    build_expr
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

cmd_projects() {
    build_expr
    find "$HOME" -mindepth 1 \
        "${EXPR[@]}" -prune -o \
        -name .git \( -type d -o -type f \) -printf '%h\0' -prune -o \
        -name '.*' -prune
}

cmd_home_roots() {
    find "$HOME" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\0'
}

cmd_landing_dir() {
    local d="${1:?landing-dir requires a path argument}"
    while [ -n "${d}" ] && [ ! -d "${d}" ]; do d="${d%/*}"; done
    printf '%s\n' "${d:-$HOME}"
}

cmd_ensure_repo() {
    local dir="${1:?ensure-repo requires a directory argument}"
    mkdir -p "${dir}"
    if ! git -C "${dir}" rev-parse --git-dir >/dev/null 2>&1; then
        git -C "${dir}" init -b main
        git -C "${dir}" config receive.denyCurrentBranch updateInstead
    fi
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

    GC_FAILED=0
    MEASURE_AS=sudo
    local machine_paths=(/var/log/journal /nix/store /root/.cache/nix)

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
    # of the 630M — is untouchable.
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
    else
        fail "skipped nix-collect-garbage -d: booted and profile generations disagree" \
            "booted:  ${booted:-<unreadable>}" \
            "profile: ${profile:-<unreadable>}" \
            "A nixos-rebuild switch half-failed (check df /boot). Re-run the switch" \
            "until both agree, then ./yo gc --yes again."
    fi

    # Outside that guard on purpose: this is nix's download cache, tied to no
    # generation, so it is safe to drop even when the profile is in the
    # broken state above — and that is exactly when the space is most
    # needed.
    step 'rm -rf /root/.cache/nix' sudo rm -rf /root/.cache/nix

    [ "${GC_FAILED}" = 0 ] || exit 1
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
    local user_paths=("$HOME/.npm" "$HOME/.local/share/containers/storage")

    if [ "${apply}" != 1 ]; then
        printf 'user tier (current size of each target):\n'
        local path
        for path in "${user_paths[@]}"; do
            measure "${path}"
        done
        if [ "${deep}" = 1 ]; then
            printf '\nproject tier (--deep):\n'
            local found=0 target
            build_targets
            while IFS= read -r -d '' target; do
                found=1
                measure "${target}"
            done <"${TARGETS_FILE}"
            rm -f "${TARGETS_FILE}"
            [ "${found}" = 1 ] || printf '  (none)\n'
        else
            printf '\nproject tier not measured; add --deep to include it.\n'
        fi
        [ "${GC_FAILED}" = 0 ] || exit 1
        exit 0
    fi

    step 'npm cache clean --force' npm cache clean --force
    # -f only: dangling containers, networks and build cache. Unused *images*
    # need a registry pull to come back, which is not regenerable offline, so
    # they wait for --deep. nix/podman.nix already runs a periodic autoPrune
    # besides.
    step 'podman system prune -f' podman system prune -f

    if [ "${deep}" = 1 ]; then
        step 'podman system prune -af' podman system prune -af
        local target
        build_targets
        while IFS= read -r -d '' target; do
            # Deleting a path git does not ignore dirties the VM worktree,
            # and receive.denyCurrentBranch=updateInstead then refuses every
            # later `git push yolobox` — a breakage that surfaces much later
            # with no link back to here. The name alone does not settle it:
            # an anchored /node_modules in .gitignore leaves a nested one
            # tracked.
            if git -C "$(dirname "${target}")" check-ignore -q "${target}"; then
                step "rm -rf ${target}" rm -rf "${target}"
            else
                fail "kept ${target}: git does not ignore it" \
                    "Deleting it would dirty the worktree and block every later" \
                    "push to the yolobox remote. Add it to that repo's .gitignore" \
                    "(or remove it by hand and commit) if it really is build output."
            fi
        done <"${TARGETS_FILE}"
        rm -f "${TARGETS_FILE}"
    fi

    printf '\nafter:\n'
    df -h /

    [ "${GC_FAILED}" = 0 ] || exit 1
}

cmd_aws_check() {
    local url="${AWS_CONTAINER_CREDENTIALS_FULL_URI:-}"
    local token="${AWS_CONTAINER_AUTHORIZATION_TOKEN:-}"
    if [ -z "${url}" ] || [ -z "${token}" ]; then
        printf 'ENV_MISSING=1\n'
        return 0
    fi
    local health_url="${url%/creds}/health"
    local health
    health="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${health_url}" || true)"
    printf 'HEALTH=%s\n' "${health}"
    local creds
    creds="$(curl -sS -H "Authorization: ${token}" --max-time 5 "${url}" || true)"
    printf 'CREDS=%s\n' "${creds}"
    local sts
    sts="$(aws sts get-caller-identity 2>&1 || true)"
    printf 'STS=%s\n' "${sts}"
}

main() {
    local sub="${1:-}"
    [ "$#" -ge 1 ] && shift
    case "${sub}" in
    projects) cmd_projects ;;
    home-roots) cmd_home_roots ;;
    landing-dir) cmd_landing_dir "$@" ;;
    ensure-repo) cmd_ensure_repo "$@" ;;
    generations) generations ;;
    gc-machine) cmd_gc_machine "$@" ;;
    gc-user) cmd_gc_user "$@" ;;
    aws-check) cmd_aws_check ;;
    *)
        printf "yolobox-guest: unknown subcommand '%s'\n" "${sub}" >&2
        exit 64
        ;;
    esac
}

main "$@"
