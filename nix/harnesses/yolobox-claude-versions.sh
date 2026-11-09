#!/usr/bin/env bash
# Lists the installed claude versions, oldest first: an executable file whose
# name is a version (2.1.268, 2.2.0-beta.1) directly under the versions
# directory. The installer drops a download under a temporary name first, and
# nothing else may be picked or pruned. Extra find tests go in "$@". No
# versions directory is no install; any other find error is shown.
# CLAUDE_VERSIONS_DIR and CLAUDE_VERSION_RE are set by the build.

[ -d "$CLAUDE_VERSIONS_DIR" ] || exit 0
find "$CLAUDE_VERSIONS_DIR" -maxdepth 1 -type f -perm -u+x \
    -regextype posix-extended -regex ".*/$CLAUDE_VERSION_RE" "$@" -printf '%f\n' |
    LC_ALL=C sort -V
