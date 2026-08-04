#!/bin/sh
# yolobox — report an in-box agent's status into the HOST herd.
#
# Generic: every harness with TOOL_REPORT=full bakes/writes a call to this
# script with its OWN two args, <state> <agent> -- e.g. Claude Code's hooks
# (tools/20-claude-code.sh's tool_configure) and pi's herd-report extension
# (tools/23-pi.sh's tool_configure) both shell out to it this way. It reports
# onto the HOST pane over the reverse-tunnelled socket the launcher set up
# (HERDR_SOCKET_PATH=/tmp/herdr/host.sock).
#
# CRITICAL: reports use the `yolobox:<agent>` source, NOT `herdr:<agent>`. The
# server RESERVES the `herdr:*` sources for its own screen/session detection of
# a real agent process, and CLEARS them for a containerized agent (whose
# foreground process is `docker`) — which is exactly why a boxed agent
# otherwise shows as `unknown`. An arbitrary source is taken at face value, so
# the box's live status sticks.
#
# Best-effort and always exit 0: a reporting failure must never disturb the
# agent.

set -u

state="${1:-}"
agent="${2:-}"

# Only when the launcher wired host-list integration for this box.
[ "${YOLOBOX_HERD:-}" = "1" ]        || exit 0
[ -n "${HERDR_PANE_ID:-}" ]          || exit 0
[ -S "${HERDR_SOCKET_PATH:-}" ]      || exit 0   # the tunnel endpoint
command -v herdr >/dev/null 2>&1     || exit 0
[ -n "${agent}" ]                    || exit 0

# A hook harness (e.g. Claude Code) may pipe the event JSON on stdin; we key
# off the state argument instead, so drain it.
cat >/dev/null 2>&1 || true

case "${state}" in
    release)
        herdr pane release-agent "${HERDR_PANE_ID}" \
            --source "yolobox:${agent}" --agent "${agent}" >/dev/null 2>&1 || true
        ;;
    working|idle|blocked)
        herdr pane report-agent "${HERDR_PANE_ID}" \
            --source "yolobox:${agent}" --agent "${agent}" --state "${state}" >/dev/null 2>&1 || true
        ;;
esac

exit 0
