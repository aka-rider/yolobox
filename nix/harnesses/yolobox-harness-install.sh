#!/usr/bin/env bash
# Installs claude and pi from their vendors, as the agent, once the network is
# up. CLAUDE_VERSIONS_LIST (the script that lists the installed claude
# versions) is set by the build; yolobox-claude-launcher-keep is on the unit's
# PATH.

cd "$HOME"
mkdir -p "$HOME/.local/bin"

# Atomic: a reader never sees the link missing.
link_bin() {
    ln -sfn "$2" "$HOME/.local/bin/$1.tmp"
    mv -T "$HOME/.local/bin/$1.tmp" "$HOME/.local/bin/$1"
}

# Each vendor is an independent phase: one vendor's outage (a down download
# host, a bad config file) must not keep the others from installing. A phase
# runs in its own errexit subshell, so its first failing command ends that
# phase and nothing else; the failure is recorded, and the unit still exits
# non-zero at the very end so Restart=on-failure retries. Every phase is
# idempotent, so a retry redoes only what is missing.
failed=()
phase() {
    local name="$1" status=0
    shift
    set +e
    (
        set -e
        "$@"
    )
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        echo "yolobox-harness-install: phase '$name' failed (exit $status)" >&2
        failed+=("$name")
    fi
}

# grep -q exits at its first match, and a producer still writing then dies
# of SIGPIPE, which pipefail turns into a failed probe. So the producer's
# whole output is captured first.
probe_has() {
    local pattern="$1" out
    shift
    out="$("$@" || true)"
    grep -q -- "$pattern" <<<"$out"
}

install_claude() {
    if [ -z "$("$CLAUDE_VERSIONS_LIST")" ]; then
        curl -fsSL --proto '=https' --tlsv1.2 https://claude.ai/install.sh | bash -s latest
    fi
    # Unconditional: the guarantee upstream documents covers updates, not the
    # initial `claude install` the installer script runs.
    yolobox-claude-launcher-keep
}

# No tty under systemd, so pi's installer takes its defaults and never edits
# an rc file. `pi update` self-updates.
install_pi() {
    [ -x "$HOME/.pi/agent/bin/pi" ] ||
        curl -fsSL --proto '=https' --tlsv1.2 https://pi.dev/install.sh | bash
    link_bin pi "$HOME/.pi/agent/bin/pi"
}

# The integration writes into pi's extensions directory, so it needs pi
# installed first. Its status probe, not a file test, decides whether it is
# current.
install_herdr_pi() {
    probe_has '^pi: current' herdr integration status || herdr integration install pi
}

phase claude install_claude
phase pi install_pi
phase herdr-pi install_herdr_pi

for tool in claude pi node; do
    phase "$tool --version" "$tool" --version
done

if [ "${#failed[@]}" -gt 0 ]; then
    printf 'yolobox-harness-install: failed phase: %s\n' "${failed[@]}" >&2
    exit 1
fi
