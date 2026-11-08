#!/usr/bin/env bash
# The box's own ~/.local/bin/claude. Claude Code >= 2.1.207 leaves a custom
# ~/.local/bin/claude alone: `claude update` and the background updater only
# drop new binaries into ~/.local/share/claude/versions/. The box owns that
# path with this launcher instead of putting a wrapper on the system PATH:
# the agent's shell rc files may prepend $HOME/.local/bin, so any wrapper
# behind it loses in every interactive shell, silently. The launcher exists
# to export HERDR_AGENT. It is built with no runtimeInputs, because it execs
# claude, whose children must keep the PATH the project's shell gave them.
# CLAUDE_VERSIONS_DIR, CLAUDE_VERSIONS_LIST (the script that lists the
# installed versions) and YOLOBOX_AGENT_USER are set by the build.

# The binaries under the versions directory are the agent's own downloads, so
# any other account running them runs agent-written code with its own
# privileges. The launcher is world-readable under /etc; this is its gate.
if [ "$(id -un)" != "$YOLOBOX_AGENT_USER" ]; then
    echo "claude: this launcher runs binaries the agent installed; run it as $YOLOBOX_AGENT_USER, not $(id -un)" >&2
    exit 1
fi

# A binary modified in the last minute may still be downloading; skip it
# unless it is all there is (a first install would otherwise have no claude
# for a minute).
newest="$("$CLAUDE_VERSIONS_LIST" ! -mmin -1 | tail -n 1)"
if [ -z "$newest" ]; then
    newest="$("$CLAUDE_VERSIONS_LIST" | tail -n 1)"
fi

if [ -z "$newest" ]; then
    echo "claude: no installed version under $CLAUDE_VERSIONS_DIR" >&2
    echo "  the vendor install runs as a user unit: systemctl --user status yolobox-harness-install" >&2
    echo "  its output: journalctl --user -u yolobox-harness-install" >&2
    exit 127
fi

# herdr's classification keys off the foreground process name, but the
# exec'd binary is named after its version ("2.1.268"), not "claude".
export HERDR_AGENT=claude
binary="$CLAUDE_VERSIONS_DIR/$newest"
# The build's variables are the launcher's, not claude's.
unset CLAUDE_VERSIONS_DIR CLAUDE_VERSIONS_LIST YOLOBOX_AGENT_USER
exec "$binary" "$@"
