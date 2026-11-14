#!/usr/bin/env bash
# Registers the box's SSH agent socket with the herdr server and holds the
# registration: herdr keeps an agent registered only while the connection that
# registered it stays open, and ends it on the first byte the client writes
# after the request. So this writes once, then only waits. It exits 1 when herdr
# is unreachable, refuses the socket, or closes the connection, with the reason
# on stderr, and systemd starts it again.
# HERDR_API_SOCK and AGENT_AUTH_SOCK are set by the build.

export LC_ALL=C

# socat's stdin is the coproc's write end, which this shell keeps open: an
# end of input would make socat half-close and leave the connection.
coproc lease { socat - UNIX-CONNECT:"$HERDR_API_SOCK"; }

request='{"id":"yolobox:ssh-agent","method":"server.ssh_agent.register","params":{"socket_path":"'"$AGENT_AUTH_SOCK"'"}}'
printf '%s\n' "$request" >&"${lease[1]}"

if ! IFS= read -r reply <&"${lease[0]}"; then
    printf 'herdr-agent-lease: herdr closed %s before replying\n' "$HERDR_API_SOCK" >&2
    exit 1
fi
if [[ $reply != *'"result":{"type":"ok"}'* ]]; then
    printf 'herdr-agent-lease: herdr refused %s: %s\n' "$AGENT_AUTH_SOCK" "$reply" >&2
    exit 1
fi

# socat ends when herdr closes the connection: the server restarted, or it
# dropped the registration.
status=0
# shellcheck disable=SC2154 # bash sets lease_PID for the coproc named lease
wait "$lease_PID" || status=$?
printf 'herdr-agent-lease: herdr ended the SSH agent lease (socat exit %s)\n' "$status" >&2
exit 1
