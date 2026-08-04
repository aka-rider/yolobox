#!/usr/bin/env bash
# yolobox entrypoint — runs UNPRIVILEGED, as PID 1, at container CREATE and on
# every subsequent `docker start`. `docker exec` (what the launcher uses for
# every actual session) does NOT run this file — see /usr/local/bin/yolobox-init
# and /usr/local/bin/yolobox-shell for what runs per-exec instead.
#
# The launcher creates this container with --user $(id -u):$(id -g),
# --cap-drop=ALL and --security-opt no-new-privileges. There is therefore NO
# privilege to do identity fixups: no chown, no usermod, no su/gosu, no sudo.
# The uid:gid were baked into the image at build time to match the host user, so
# nothing needs adjusting here.
#
# Responsibilities, in order:
#   1. Announce the effective identity (diagnostic, to stderr).
#   2. Run yolobox-init, but ONLY if the launcher isn't going to (see
#      YOLOBOX_MANAGED below) — a create/start not driven by the launcher (e.g.
#      a raw `docker start`) still gets cache-dir/per-module seeding.
#   3. Start the box's loopback-only sshd, UNCONDITIONALLY — a create-time value
#      can't depend on whether this particular session happens to run inside a
#      herd pane, since `docker exec` never re-runs this file (see the comment
#      below the sshd block for the full rationale).
#   4. exec the requested command, or yolobox-shell for the default handoff.
#
# HOME (/home/<user>) lives on the yolobox-home Docker named volume — native
# ext4 in the Docker Desktop VM disk image, not a host bind mount. On the very
# first run it may be empty; nothing below assumes any pre-existing state.

set -uo pipefail

# HOME is pinned by the image ENV, but a numeric --user can still surprise us;
# force it so the shell and every tool resolve the writable volume home rather
# than a read-only "/".
export HOME="${HOME:-/home/${YOLOBOX_USER:-dev}}"

# herdr's Unix socket lives on /tmp (a tmpfs), independent of $HOME.
if [ -n "${HERDR_SOCKET_PATH:-}" ]; then
    mkdir -p "$(dirname "${HERDR_SOCKET_PATH}")" 2>/dev/null || true
fi

# --- 1. Identity diagnostic -------------------------------------------------
echo "yolobox uid=$(id -u) gid=$(id -g)" >&2

# --- 2. Per-session init, unless the launcher will run it itself -----------
# YOLOBOX_MANAGED=1 is set by the launcher at container CREATE time (constant
# across invocations, so it never perturbs CONFIG_HASH). When set, the launcher
# runs yolobox-init synchronously via `docker exec` on every invocation instead
# — running it here too would race that copy: two concurrent read-modify-writes
# of a harness's seeded settings file, or a per-module seed script running
# twice at once against the same $HOME.
if [ "${YOLOBOX_MANAGED:-}" != "1" ]; then
    /usr/local/bin/yolobox-init \
        || echo "yolobox: yolobox-init exited non-zero; continuing" >&2
fi

# --- 3. Box sshd — unconditional -------------------------------------------
# Loopback-only, pubkey-only sshd for the herd reverse-report tunnel. Started
# unconditionally (not gated on YOLOBOX_HERD): `docker exec` never runs this
# file, so an exec-time YOLOBOX_HERD could never start it; and a create-time
# value would put "was this launched from a herd pane?" into CONFIG_HASH,
# destroying the box every time you alternate between a herd pane and a plain
# terminal. It is safe unconditionally: authorized_keys is seeded by the
# launcher from the host 1Password agent's presented keys (empty otherwise, so
# it accepts nobody by default), and YOLOBOX_HERD continues to gate *reporting*
# only — the Claude Code hooks baked into /etc/claude-code/managed-settings.json
# — passed per-invocation via `docker exec -e`.
mkdir -p /tmp/herdr
mkdir -p "$HOME/.ssh"
# Persistent host key (generated once; survives on the /home volume).
if [ ! -f "$HOME/.ssh/ssh_host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$HOME/.ssh/ssh_host_ed25519_key" 2>/dev/null || true
fi
if [ -f "$HOME/.ssh/ssh_host_ed25519_key" ] && command -v /usr/sbin/sshd >/dev/null 2>&1; then
    # AllowTcpForwarding=remote is required: it (not AllowStreamLocalForwarding
    # alone) is the gate for the reverse `-R` forward, even for a Unix socket.
    # Restricting both to `remote` permits only the host's inbound report tunnel.
    /usr/sbin/sshd -D -e -p 2222 -h "$HOME/.ssh/ssh_host_ed25519_key" \
        -o UsePAM=no -o PidFile=none -o StrictModes=no \
        -o PasswordAuthentication=no -o PubkeyAuthentication=yes \
        -o AllowTcpForwarding=remote -o AllowStreamLocalForwarding=remote \
        -o AllowAgentForwarding=no -o X11Forwarding=no &
    echo "yolobox: sshd up on :2222 (herd report tunnel endpoint)" >&2
else
    echo "yolobox: sshd unavailable; box agent won't appear in the host herd" >&2
fi

# --- 4. Hand off -------------------------------------------------------------
# Any command after the image name on the docker CLI arrives here as "$@" — only
# relevant to a raw `docker run`/`docker start --attach` that bypasses the
# launcher. The launcher itself always talks to this container over
# `docker exec` (see yolobox-shell), never through this handoff.
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec /usr/local/bin/yolobox-shell
fi
