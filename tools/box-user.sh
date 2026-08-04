#!/usr/bin/env bash
# Print the sanitised in-box username. Linux usernames: lowercase alnum, '_' and
# '-', not starting with a digit, <=32 chars.
# NOTE the `tr -d '\n'` FIRST: without it, `tr -c 'a-z0-9_-' '_'` converts id's
# trailing newline into a literal '_' (it is then no longer a newline, so command
# substitution does not strip it) and every username silently gains a trailing
# underscore — `xiii` becomes `xiii_`, and $HOME becomes /home/xiii_.
set -euo pipefail
u="$(id -un | tr -d '\n' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '_' | cut -c1-32)"
case "${u}" in ''|[0-9]*) u=dev ;; esac
printf '%s\n' "${u}"
