#!/usr/bin/env bash
# Re-asserts the box's claude launcher at the agent's ~/.local/bin/claude and
# prunes old claude versions. CLAUDE_LAUNCHER, CLAUDE_LAUNCHER_LINK,
# CLAUDE_VERSIONS_DIR and CLAUDE_VERSIONS_LIST (the script that lists the
# installed versions) are set by the build.

keep_versions=2
settle_minutes=10

if [ "$(readlink "$CLAUDE_LAUNCHER_LINK" || true)" != "$CLAUDE_LAUNCHER" ]; then
    mkdir -p "$(dirname "$CLAUDE_LAUNCHER_LINK")"
    ln -sfn "$CLAUDE_LAUNCHER" "$CLAUDE_LAUNCHER_LINK.tmp"
    mv -T "$CLAUDE_LAUNCHER_LINK.tmp" "$CLAUDE_LAUNCHER_LINK"
    echo "relinked $CLAUDE_LAUNCHER_LINK -> $CLAUDE_LAUNCHER"
fi

# Rank every version first, so a fresh download counts toward the
# $keep_versions kept (the launcher runs the newest, which ranking always
# spares). Only then does settling filter the rest: one the updater finished
# with more than $settle_minutes minutes ago, so a download in flight is
# never a pruning target. Nothing else prunes them: keeping every version is
# precisely what the custom launcher buys, at ~330 MB each.
older="$("$CLAUDE_VERSIONS_LIST" | head -n "-$keep_versions")"
if [ -n "$older" ]; then
    while IFS= read -r version; do
        if [ -n "$(find "$CLAUDE_VERSIONS_DIR/$version" -maxdepth 0 -mmin "+$settle_minutes" -print)" ]; then
            rm -f "$CLAUDE_VERSIONS_DIR/$version"
            echo "pruned $CLAUDE_VERSIONS_DIR/$version"
        fi
    done <<<"$older"
fi
